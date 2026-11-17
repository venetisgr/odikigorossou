# Στάδιο 1: κατεβάζει τα κείμενα από το lexifina/greece-law και χτίζει το ευρετήριο (~10 λεπτά).
FROM python:3.12-slim AS index
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /build
COPY pyproject.toml ./
COPY src ./src
COPY tools ./tools
RUN pip install --no-cache-dir .
# Κλάδος ή commit του greece-law. Καρφώστε commit για επαναλήψιμο build: --build-arg CORPUS_REF=<sha>
ARG CORPUS_REF=main
RUN tools/fetch_corpus.sh /corpus "$CORPUS_REF" \
    && python -m dikigoros.indexer --source /corpus --out /build/index.db \
    && rm -rf /corpus

# Στάδιο 2: η υπηρεσία, με το ευρετήριο μέσα στο image (χωρίς volume, χωρίς βάση).
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 DIKIGOROS_INDEX=/app/data/index.db
WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir .
COPY --from=index /build/index.db /app/data/index.db
RUN useradd --system --no-create-home app
USER app
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4)"
# Οι απαντήσεις είναι ροές 20–90 δευτερολέπτων: στον τερματισμό περιμένουμε να ολοκληρωθούν.
CMD ["uvicorn", "dikigoros.server:app", "--host", "0.0.0.0", "--port", "8000", "--timeout-graceful-shutdown", "110"]
