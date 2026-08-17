FROM python:3.11-slim

ARG OPENZLAGENT_ARCHIVE=https://github.com/Kkkirito-123/OpenZLAgent/archive/3e1371b17331f9349973dfbfcd5e92ad16d1ccac.zip

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /srv/openzltravel

COPY backend/requirements.txt backend/requirements-loadtest.txt /tmp/openzltravel/
RUN python -m pip install --no-cache-dir "${OPENZLAGENT_ARCHIVE}" \
    && python -m pip install --no-cache-dir -r /tmp/openzltravel/requirements-loadtest.txt

COPY backend/app ./app
COPY backend/loadtests ./loadtests

CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "4"]
