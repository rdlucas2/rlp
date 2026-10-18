FROM python:3.13-slim AS build
RUN pip install --no-cache-dir uv

FROM python:3.13-slim
COPY --from=build /usr/local /usr/local
USER 10001
CMD ["python", "-m", "app"]
