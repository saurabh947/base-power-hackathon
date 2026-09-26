# Cloud targets (integrator). Images build on Cloud Build, not on the laptop.
.PHONY: cloud-deploy cloud-ingest cloud-forecast cloud-predict cloud-backtest cloud-migrate cloud-url

cloud-deploy:           ## build + deploy the Cloud Run service (optional CORS=https://<site>.vercel.app)
	scripts/cloud/deploy.sh $(CORS)

cloud-ingest:           ## load ERCOT data into Cloud SQL (Cloud Run job); INGEST_YEARS="2026" refreshes the current year
	INGEST_YEARS="$(or $(INGEST_YEARS),2024 2025 2026)" scripts/cloud/jobs.sh ingest

cloud-forecast:         ## build the forecast image and run the TimesFM job (YEAR=2025 by default)
	YEAR=$(or $(YEAR),2025) scripts/cloud/jobs.sh forecast

cloud-predict:          ## the AI's prediction for the rest of YEAR (2026) from real_until_utc (forecast image)
	YEAR=$(or $(YEAR),2026) SKIP_BUILD=$(or $(SKIP_BUILD),0) scripts/cloud/jobs.sh predict

cloud-backtest:         ## full-year pitch backtest into Cloud SQL (needs forecasts first)
	scripts/cloud/jobs.sh backtest

cloud-migrate:          ## apply DB migrations in Cloud SQL (Cloud Run job)
	gcloud run jobs execute fleet-sim-migrate --project appintellect --region us-east1 --wait

cloud-url:
	@gcloud run services describe fleet-sim --project appintellect --region us-east1 --format='value(status.url)'
