"""fastapp — the FastAPI sidecar agri-api migrates into (strangler fig).

Lives in the SAME pyproject / Docker image as the Django app and reads the
SAME environment variables, so both processes always see identical config.
uvicorn serves it on :8001 (`docker-entrypoint.sh fast`); nginx on the
droplet cuts path prefixes over from Django (:8000) one at a time — see
deploy/nginx/back.conf.
"""
