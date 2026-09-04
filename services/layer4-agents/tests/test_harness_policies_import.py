def test_harness_policies_imports_with_bare_harness_namespace():
    """Ensure harness.policies works when harness is loaded as a top-level package."""
    import sys
    from pathlib import Path
    src = Path(__file__).parent.parent / "src"
    sys.path.insert(0, str(src / "layer4_agents"))
    try:
        import layer4_agents.harness.policies  # noqa: F401
    finally:
        sys.path.pop(0)
