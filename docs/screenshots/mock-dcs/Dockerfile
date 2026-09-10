# Mock EUMETSAT DCS Web Service for the documentation screenshot pipeline.
# Stdlib only: no dependency to pin, nothing to install, starts instantly.
FROM python:3.12-alpine
WORKDIR /srv
COPY server.py /srv/server.py
EXPOSE 80
CMD ["python", "/srv/server.py"]
