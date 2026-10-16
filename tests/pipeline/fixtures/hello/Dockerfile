FROM python:3.13-slim
# The app needs no installer at runtime. Removing pip and setuptools drops them
# and the libraries pip vendors from the image, so the trivy gate scans only
# what the app ships.
RUN python -m pip uninstall --yes --no-input pip setuptools
WORKDIR /app
COPY src/ /app/
USER 10001
CMD ["python", "-m", "hello"]
