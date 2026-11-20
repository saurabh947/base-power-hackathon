# Shared settings for the cloud scripts. Sourced, not executed.
P=appintellect
R=us-east1
REPO=us-east1-docker.pkg.dev/appintellect/fleet-sim
SA=fleet-sim-run@appintellect.iam.gserviceaccount.com
# Cloud SQL, for the jobs only (scripts/cloud/jobs.sh). The service has needed no database since 2026-11-19, when
# fleet-sim-db was exported to gs://appintellect-fleet-sim-backup and deleted, along with the fleet-sim-db-password
# secret and the Cloud Run jobs. Running a job again needs a new database and secret first.
SQL=appintellect:us-east1:fleet-sim-db
DB_ENV="DB_HOST=/cloudsql/$SQL,DB_NAME=fleetsim,DB_USER=fleetsim"
DB_SECRET="DB_PASSWORD=fleet-sim-db-password:latest"
TAG="${TAG:-$(git rev-parse --short HEAD 2>/dev/null || date +%Y%m%d%H%M%S)}"   # override to reuse a built image
