# Use an official Python runtime as a parent image
FROM python:3.12-slim

# Set the working directory in the container
WORKDIR /app

# Refuse an image built by anything but this repo's cloudbuild.yaml, which
# passes --build-arg BUILD_CONFIG=cloudbuild.yaml. The inline build the Cloud
# Run wizard stored in the trigger never does, so it fails here, before any
# push or deploy, instead of shipping a service whose Cloud Run jobs were not
# refreshed (the old revision keeps serving). Local: docker build
# --build-arg BUILD_CONFIG=cloudbuild.yaml server
ARG BUILD_CONFIG
RUN [ "$BUILD_CONFIG" = "cloudbuild.yaml" ] || { \
      echo "ERROR: built without cloudbuild.yaml (BUILD_CONFIG='$BUILD_CONFIG')."; \
      echo "The Cloud Build trigger still uses its inline build. Switch it once to"; \
      echo "cloudbuild.yaml: see 'GCP Cloud Run Jobs' in server/ARCHITECTURE.md."; \
      exit 1; }

# Copy the current directory contents into the container at /app
COPY . /app

# Install any needed packages specified in requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Precompile the app's .py files to bytecode. The build context (a git
# checkout) has no __pycache__, so without this each new instance compiles
# every module it imports before gunicorn's worker can serve a request.
# compileall walks all of /app, so a syntax error in any file, tests and job
# scripts included, fails the build here instead of shipping.
RUN python -m compileall -q -j 0 /app

# Make port 8080 available to the world outside this container
EXPOSE 8080

# The commit this image was built from (cloudbuild.yaml passes it):
# job_predeploy.py refuses to check an image of another commit.
ARG COMMIT_SHA
ENV BUILD_COMMIT_SHA=$COMMIT_SHA

# Run Gunicorn only: the Pub/Sub topic check and the index builds run once per
# deploy in the Cloud Run job <service>-predeploy (job_predeploy.py), which
# cloudbuild.yaml runs before it deploys this image, not on every cold start.
# exec makes gunicorn PID 1, so Cloud Run's SIGTERM reaches it. --timeout 120:
# the 30 s default can kill a worker while it imports the app. --threads 8:
# one worker process (one MongoClient, which is thread-safe: the connection
# budget is unchanged) serves 8 requests at once, so a chat turn waiting on
# the model does not block the others.
CMD ["sh", "-c", "exec gunicorn -b 0.0.0.0:8080 --timeout 120 --threads 8 app:app"]
