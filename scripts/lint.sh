uv run black src/ tests/

# Lint code
uv run ruff check src/ tests/ --unsafe-fixes --fix

# Custom anti-pattern checks (not covered by ruff)
uv run python scripts/check_antipatterns.py