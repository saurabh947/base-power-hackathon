# Cloud targets (integrator). Images build on Cloud Build, not on the laptop.
.PHONY: cloud-deploy cloud-ingest cloud-forecast cloud-backtest cloud-migrate cloud-url

cloud-deploy:           ## build + deploy the Cloud Run service (optional CORS=https://<site>.vercel.app)
	scripts/cloud/deploy.sh $(CORS)

cloud-ingest:           ## load ERCOT data into Cloud SQL (Cloud Run job)
	scripts/cloud/jobs.sh ingest

cloud-forecast:         ## build the forecast image and run the TimesFM job
	scripts/cloud/jobs.sh forecast

cloud-backtest:         ## full-year pitch backtest into Cloud SQL (needs forecasts first)
	scripts/cloud/jobs.sh backtest

cloud-migrate:          ## apply DB migrations in Cloud SQL (Cloud Run job)
	gcloud run jobs execute fleet-sim-migrate --project appintellect --region us-east1 --wait

cloud-url:
	@gcloud run services describe fleet-sim --project appintellect --region us-east1 --format='value(status.url)'
