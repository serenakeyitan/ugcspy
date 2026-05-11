"""Smoke tests — replaced by real tests as each script is implemented."""


def test_imports() -> None:
    from scripts import assemble_recipe, detect_cuts, download, extract_keyframes  # noqa: F401
