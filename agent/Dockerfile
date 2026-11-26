FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /srv

COPY pyproject.toml ./
COPY heater_agent ./heater_agent
RUN pip install --no-cache-dir .

# Läuft als root im Container: BlueZ erlaubt den D-Bus-Zugriff standardmäßig nur
# root bzw. der Gruppe "bluetooth" des Hosts. Der Container bekommt nur den
# D-Bus-Socket des Hosts, keine weiteren Rechte.
WORKDIR /config
CMD ["heater-agent", "-c", "/config/config.yaml"]
