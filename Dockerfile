# Imagem enxuta: Python slim + uv (binário oficial, sem pip install de uv).
FROM python:3.12-slim

# Copia o uv da imagem oficial (pinado para builds reproduzíveis).
COPY --from=ghcr.io/astral-sh/uv:0.11.21 /uv /uvx /bin/

# Ambiente previsível: logs em tempo real e uv sem hardlink (camadas Docker).
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PROJECT_ENVIRONMENT=/app/.venv

WORKDIR /app

# 1) Só os manifests primeiro: a camada de dependências fica em cache e só é
#    refeita quando pyproject.toml / uv.lock mudam.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# 2) Código + templates + dados (NUNCA o .env — barrado pelo .dockerignore).
COPY src ./src
COPY templates ./templates
COPY data ./data

# Roda como usuário não-root (boa prática de segurança).
RUN useradd --create-home app && chown -R app:app /app
USER app

# Entrypoint: pipeline ponta a ponta. Flags podem ir no `docker run`
# (ex.: `docker run ... --dry-run` ou `--no-notify`).
ENTRYPOINT ["uv", "run", "--frozen", "--no-dev", "python", "-m", "src.main"]
