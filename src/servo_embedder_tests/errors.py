class ScenarioError(RuntimeError):
    """The harness or the device failed, as opposed to the app under test."""


class ScenarioTimeout(AssertionError):
    """A wait in a scenario ran out of time. Reported as TIMEOUT."""


class AppCrashed(AssertionError):
    """The app under test exited during the test. Reported as CRASH."""
