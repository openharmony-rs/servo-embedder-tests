"""The page scenarios in the folders that --page-scenarios names, a subfolder each.

`test_renders` checks that a page gets ready, settles and passes its own checks (`expect_change`, recorded errors,
the seeded `Math.random()`).
`test_matches_reference` compares the captures of --engine with those of --reference-engine, stored in --baselines or
rendered in the run (and then stored), and `test_matches_baseline` with its baselines in --baselines, noting for each
change whether it moved toward the stored captures of --reference-engine. pytest runs the tests of a module in the
order they are defined, so the references are stored before the regression checks judge changes with them. Each page
is rendered once per engine for all three. Differences are described in each test's `diff.json`: the comparison tests
decide at once, and describing the differences, the slow part, goes on in the background until the end of the run.
"""

import pytest

from servo_embedder_tests.page_scenarios import compare_captures, discover

pytestmark = pytest.mark.explicit


def pytest_generate_tests(metafunc):
    if "scenario" in metafunc.fixturenames:
        scenarios = discover(metafunc.config.getoption("page_scenarios"))
        metafunc.parametrize("scenario", scenarios, ids=[scenario.name for scenario in scenarios])


def test_renders(scenario, page_runs):
    rendering = page_runs.rendering(page_runs.engine, scenario)
    rendering.raise_error()
    assert not rendering.problems, "; ".join(rendering.problems)


def test_matches_reference(scenario, page_runs, artifacts):
    if page_runs.reference is None:
        pytest.skip("needs --reference-engine")
    found = page_runs.reference_captures(scenario)
    if found is None:
        pytest.skip(f"no stored rendering by {page_runs.reference} and no engine of that name to render one")
    reference, origin = found
    rendering = page_runs.rendering(page_runs.engine, scenario)
    rendering.raise_error()
    comparison = compare_captures(
        reference,
        rendering,
        scenario.preset("reference"),
        artifacts,
        (page_runs.reference, page_runs.engine),
        describe=lambda: {"expected": origin, "actual": rendering.info.get("engine")},
        describer=page_runs.describer,
    )
    assert comparison.matches, comparison.message(f"{page_runs.reference}'s rendering")


def test_matches_baseline(scenario, page_runs, artifacts):
    store = page_runs.store(page_runs.engine)
    if store is None:
        pytest.skip("needs --baselines")
    rendering = page_runs.rendering(page_runs.engine, scenario)
    rendering.raise_error()
    if page_runs.update_baselines:
        store.put(rendering, reason="--update-baselines")
        return
    expected = store.captures(scenario.name)
    if not expected:
        pytest.fail(
            f"no baseline in {store.directory(scenario.name)}; store one with --update-baselines or "
            "`servo-embedder-tests accept`"
        )
    reference, origin = page_runs.stored_reference(scenario)
    comparison = compare_captures(
        expected,
        rendering,
        scenario.preset("baseline"),
        artifacts,
        ("baseline", page_runs.engine),
        reference,
        lambda: {
            "expected": store.origin(scenario.name),
            "actual": rendering.info.get("engine"),
            **({"reference": origin} if origin else {}),
        },
        describer=page_runs.describer,
    )
    assert comparison.matches, comparison.message(f"the baseline in {store.directory(scenario.name)}")
