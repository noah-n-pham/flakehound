"""Finding candidate repositories for the observational board.

Discovery answers "which repositories should we even look at", and `app/observe.py`
answers "may we watch this one". Keeping them apart is what makes the admission list
auditable: a search query that returned the wrong sort of repository shows up as a low
admission rate rather than as a board full of junk.

**Three star bands, because the brief asks for a balanced pool** — personal and
small-project repositories below 100 stars, lower-popularity ones from 100 to 300, and
medium ones from 300 to 500. Stars are a *discovery aid only*: nothing in
`observe.assess()` reads them, and a repository is admitted or refused on activity and
history alone. Deliberately no huge famous repositories; the bands stop at 500.

**Search has its own rate limit and its own bucket, and that is not a detail.** An
installation token gets 5,000 core requests an hour but only **30 searches a minute**,
on a separate `search` resource. Sending search through the core limiter would be worse
than merely wrong: `RateLimiter.observe()` believes `x-ratelimit-limit`, so one search
response would clamp the core bucket's capacity from 5,000 to 30 and throttle the
installed backfill to a trickle. Hence a second limiter, constructed for search's
window, passed explicitly.

**Ordered by `updated`, never by anything to do with outcomes.** The pool must not be
assembled in an order that correlates with flakiness, or the board would be reporting
its own search ordering. Recently-pushed-first correlates with *activity*, which is
already an eligibility criterion rather than a hidden one.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.github import api_request
from app.logging import get_logger
from app.models import Repository
from app.observe import (
    PUSHED_WITHIN_DAYS,
    REQUESTS_PER_CANDIDATE,
    CandidateFacts,
    Verdict,
    screen,
)
from app.upserts import upsert_repository

log = get_logger(__name__)


@dataclass(frozen=True)
class Band:
    """One popularity band. `low` and `high` are inclusive, as GitHub reads them."""

    name: str
    low: int
    high: int

    @property
    def stars_filter(self) -> str:
        return f"stars:{self.low}..{self.high}"


# The brief's three bands. The floor of 5 on the smallest band is a **discovery
# efficiency** choice and not an eligibility rule: below it the results are
# overwhelmingly repositories with no Actions at all, and screening each one costs three
# requests to learn nothing. It cannot bias the board, because star count is invisible
# to `assess()` — it only changes how many candidates are wasted.
BANDS = (
    Band(name="small", low=5, high=99),
    Band(name="lower", low=100, high=299),
    Band(name="medium", low=300, high=500),
)

# Start conservatively, per the brief: discover more than are needed, crawl a bounded
# sample, expand only if the board is thin. At three requests a candidate this is
# 3 × 30 × 3 = 270 core requests for a full pass.
CANDIDATES_PER_BAND = 30
SEARCH_PAGE_SIZE = 100
# Search caps any query at 1,000 reachable results; 100 per page is ten pages.
SEARCH_MAX_PAGES = 10
# Search's own budget, which is nothing like core's.
SEARCH_LIMIT_PER_MINUTE = 30
SEARCH_WINDOW_SECONDS = 60.0

_search_limiter = None


def get_search_limiter():
    """A limiter shaped for search's 30-a-minute window, not core's 5,000-an-hour.

    Module-level for the same reason the core limiter is: its whole value is what the
    last response said, so it has to outlive one call.
    """
    global _search_limiter
    if _search_limiter is None:
        from app.config import get_settings
        from app.ratelimit import RateLimiter

        _search_limiter = RateLimiter(
            max_wait_seconds=get_settings().github_rate_limit_max_wait_seconds,
            default_limit=SEARCH_LIMIT_PER_MINUTE,
            window_seconds=SEARCH_WINDOW_SECONDS,
        )
    return _search_limiter


def reset_search_limiter() -> None:
    """For tests."""
    global _search_limiter
    _search_limiter = None


def band_query(band: Band, *, now: datetime) -> str:
    """The search query for one band.

    Every clause mirrors a criterion in `observe.assess()`. That is deliberate
    duplication: the query is an optimisation that stops us spending three requests to
    learn something search already knew, and `assess()` remains the only thing that
    decides. A repository search claims is public and unarchived still has to prove it.
    """
    pushed_since = (now - timedelta(days=PUSHED_WITHIN_DAYS)).date().isoformat()
    return " ".join(
        (
            band.stars_filter,
            f"pushed:>={pushed_since}",
            "is:public",
            "archived:false",
            "fork:false",
        )
    )


def _hit_is_stored(item: dict, ids: set[int], names: set[str]) -> bool:
    """Skip by GitHub repo id when search sent one, otherwise by full_name.

    Identity is the id (Checkpoint 1). full_name is the fallback for a stub that
    only carried a name, and for a rename that has not yet been written back.
    """
    raw_id = item.get("id")
    if raw_id is not None and int(raw_id) in ids:
        return True
    name = item.get("full_name")
    return bool(name) and name in names


async def stored_repo_keys(session: AsyncSession) -> tuple[set[int], set[str]]:
    """Every repository already on disk, installed or observed.

    Screening one of these again spends three core requests to learn a verdict
    we already acted on. A later pass has to page *past* them, not re-spend.
    """
    ids: set[int] = set()
    names: set[str] = set()
    for repo_id, full_name in await session.execute(
        select(Repository.id, Repository.full_name)
    ):
        ids.add(int(repo_id))
        names.add(full_name)
    return ids, names


async def search_band(
    band: Band,
    *,
    installation_id: int,
    wanted: int,
    now: datetime | None = None,
    skip_ids: set[int] | None = None,
    skip_names: set[str] | None = None,
) -> tuple[list[str], int]:
    """Unknown full names from one band, most recently pushed first, capped at `wanted`.

    Already-stored hits are skipped and paging continues until `wanted` unknown
    names are collected or search is exhausted. That is what lets a larger pass
    fill a quota without re-screening the pool we already have. Search caps any
    query at 1,000 reachable results, so no windowing is needed here the way it
    is for the runs listing.
    """
    now = now or datetime.now(UTC)
    skip_ids = skip_ids or set()
    skip_names = skip_names or set()
    query = band_query(band, now=now)
    found: list[str] = []
    skipped = 0
    page = 1
    # When we expect to skip, take full pages so one stored hit does not waste
    # a request that only asked for `wanted` names.
    page_size = SEARCH_PAGE_SIZE if (skip_ids or skip_names) else min(SEARCH_PAGE_SIZE, wanted)

    while len(found) < wanted and page <= SEARCH_MAX_PAGES:
        requested = (
            page_size if (skip_ids or skip_names) else min(page_size, wanted - len(found))
        )
        response = await api_request(
            installation_id,
            "GET",
            "/search/repositories",
            params={
                "q": query,
                "sort": "updated",
                "order": "desc",
                "per_page": requested,
                "page": page,
            },
            limiter=get_search_limiter(),
        )
        response.raise_for_status()
        items = response.json().get("items") or []
        if not items:
            break
        for item in items:
            if _hit_is_stored(item, skip_ids, skip_names):
                skipped += 1
                continue
            found.append(item["full_name"])
            if len(found) >= wanted:
                break
        page += 1
        if len(items) < requested:
            break

    log.info(
        "discover.searched",
        band=band.name,
        query=query,
        candidates=len(found[:wanted]),
        skipped=skipped,
        pages=page - 1,
    )
    return found[:wanted], skipped


@dataclass
class BandResult:
    """What one band contributed, and what it cost."""

    band: str
    screened: int = 0
    skipped: int = 0
    admitted: list[str] = field(default_factory=list)
    rejected: dict[str, int] = field(default_factory=dict)

    @property
    def requests_spent(self) -> int:
        return self.screened * REQUESTS_PER_CANDIDATE


@dataclass
class DiscoveryResult:
    bands: list[BandResult] = field(default_factory=list)

    @property
    def admitted(self) -> list[str]:
        return [name for band in self.bands for name in band.admitted]

    @property
    def screened(self) -> int:
        return sum(band.screened for band in self.bands)

    @property
    def skipped(self) -> int:
        return sum(band.skipped for band in self.bands)

    @property
    def rejections(self) -> dict[str, int]:
        """Every reason that fired, across every band. The audit trail's summary."""
        totals: dict[str, int] = {}
        for band in self.bands:
            for reason, count in band.rejected.items():
                totals[reason] = totals.get(reason, 0) + count
        return dict(sorted(totals.items(), key=lambda item: (-item[1], item[0])))


async def _record(session: AsyncSession, facts: CandidateFacts) -> None:
    """Store one admitted repository as an observed row. Caller commits."""
    await upsert_repository(
        session,
        installation_id=None,
        source="observed",
        repository={
            "id": facts.repo_id,
            "full_name": facts.full_name,
            "name": facts.full_name.rpartition("/")[2],
            "owner": {"login": facts.full_name.split("/")[0]},
            "private": facts.private,
            "default_branch": facts.default_branch,
        },
    )


def _count(result: BandResult, verdict: Verdict) -> None:
    for reason in verdict.reasons:
        result.rejected[reason] = result.rejected.get(reason, 0) + 1


async def discover(
    session: AsyncSession,
    *,
    installation_id: int,
    bands: tuple[Band, ...] = BANDS,
    per_band: int = CANDIDATES_PER_BAND,
    now: datetime | None = None,
) -> DiscoveryResult:
    """Search each band, screen unknown candidates, persist the admitted ones.

    Already-stored repositories are skipped before the three screening requests,
    and search pages past them so `per_band` is a quota of *new* candidates.
    Screening is what decides; search only proposes. The rejection counts are
    returned rather than discarded, because "we looked at ninety and admitted
    eleven" is the honest answer to how the board's repositories were chosen.
    Each band is committed as it finishes so a crash mid-pass keeps the bands
    that already landed.
    """
    now = now or datetime.now(UTC)
    skip_ids, skip_names = await stored_repo_keys(session)
    result = DiscoveryResult()

    for band in bands:
        band_result = BandResult(band=band.name)
        names, skipped = await search_band(
            band,
            installation_id=installation_id,
            wanted=per_band,
            now=now,
            skip_ids=skip_ids,
            skip_names=skip_names,
        )
        band_result.skipped = skipped
        for full_name in names:
            facts, verdict = await screen(
                full_name, installation_id=installation_id, now=now
            )
            band_result.screened += 1
            if verdict.eligible and facts is not None:
                await _record(session, facts)
                band_result.admitted.append(facts.full_name)
                skip_ids.add(facts.repo_id)
                skip_names.add(facts.full_name)
            else:
                _count(band_result, verdict)
        result.bands.append(band_result)
        await session.commit()
        log.info(
            "discover.band_complete",
            band=band.name,
            screened=band_result.screened,
            skipped=band_result.skipped,
            admitted=len(band_result.admitted),
            rejected=band_result.rejected,
            requests_spent=band_result.requests_spent,
        )

    log.info(
        "discover.complete",
        screened=result.screened,
        skipped=result.skipped,
        admitted=len(result.admitted),
        rejections=result.rejections,
    )
    return result


async def _main(per_band: int) -> None:
    from app.db import dispose_engine, get_sessionmaker
    from app.observe import observation_installation_id

    installation_id = observation_installation_id()
    async with get_sessionmaker()() as session:
        result = await discover(session, installation_id=installation_id, per_band=per_band)
        observed = await session.scalar(
            select(func.count()).select_from(Repository).where(Repository.source == "observed")
        )
    log.info(
        "discover.committed",
        admitted=result.admitted,
        screened=result.screened,
        skipped=result.skipped,
        rejections=result.rejections,
        observed_total=int(observed or 0),
    )
    await dispose_engine()


def main() -> None:
    """`python -m app.discover [--per-band 30]` — build the candidate pool.

    Writes only `repositories` rows. Crawling their history is a separate step, so an
    admission decision is never entangled with what a crawl found.
    """
    import argparse
    import asyncio

    from app.logging import configure_logging

    configure_logging()
    parser = argparse.ArgumentParser(description="Discover public repos for the board.")
    parser.add_argument("--per-band", type=int, default=CANDIDATES_PER_BAND)
    args = parser.parse_args()
    asyncio.run(_main(args.per_band))


if __name__ == "__main__":
    main()
