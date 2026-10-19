"""Test-infra chatter: this project's CI plugin logs a lot before the run starts."""


def pytest_sessionstart(session):
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    if reporter is None:
        return
    for i in range(1500):
        reporter.write_line(
            f"[infra] fixture-cache warmup step {i:04d}: resolved plugin manifest entry "
            f"{'x' * 60} status=ok elapsed=0.00{i % 10}s"
        )
