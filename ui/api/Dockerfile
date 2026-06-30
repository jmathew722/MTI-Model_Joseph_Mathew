# ── Stage 1: compile LibreDWG (provides dwg2dxf) ──────────────────────────────
# libredwg-utils has no arm64 prebuilt package in Debian/Ubuntu repos;
# we compile from source using the official GNU tarball.
FROM debian:bookworm-slim AS dwg-builder

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential ca-certificates wget xz-utils pkg-config && \
    rm -rf /var/lib/apt/lists/*

# Download via plain HTTP to avoid SSL cert issues in slim image
RUN wget -q http://ftp.gnu.org/gnu/libredwg/libredwg-0.12.5.tar.xz && \
    tar xf libredwg-0.12.5.tar.xz && \
    cd libredwg-0.12.5 && \
    ./configure --prefix=/opt/libredwg \
        --without-perl --without-python --without-pcre2 && \
    make -j$(nproc) && \
    make install

# ── Stage 2: Python API ────────────────────────────────────────────────────────
FROM python:3.11-slim-bookworm

# fonts-dejavu-core gives ezdxf a fallback TTF font for DXF text entities;
# without it ezdxf raises FontNotFoundError on any drawing that has MTEXT.
RUN apt-get update && apt-get install -y --no-install-recommends \
    fonts-dejavu-core && \
    rm -rf /var/lib/apt/lists/*

# Copy dwg2dxf binary and the shared library it links against
COPY --from=dwg-builder /opt/libredwg/bin/dwg2dxf    /usr/local/bin/dwg2dxf
COPY --from=dwg-builder /opt/libredwg/lib/libredwg.so* /usr/local/lib/
RUN ldconfig

RUN pip install --no-cache-dir \
    flask ezdxf matplotlib pillow \
    anthropic pydantic python-dotenv rich \
    pdf2image PyMuPDF numpy

WORKDIR /app
COPY app.py .

EXPOSE 5000
CMD ["python", "app.py"]
