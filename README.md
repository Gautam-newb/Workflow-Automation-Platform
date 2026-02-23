## Workflow Automation Platform — n8n + Power Automate + AWS Lambda

This repository implements a production-style, event-driven workflow automation platform. It demonstrates how low-code tools such as **n8n** (and conceptually Power Automate) can orchestrate cloud-native automation via **AWS API Gateway**, **AWS Lambda**, and **Amazon S3**, with CI/CD powered by **GitHub Actions** and infrastructure defined using the **Serverless Framework**.

The core flow is:

- n8n receives inbound HTTP requests (for example, from SaaS systems or custom apps).
- n8n forwards a validated JSON payload to an API Gateway webhook endpoint.
- API Gateway invokes a Python 3.10 Lambda function.
- The Lambda validates the payload, enriches it with metadata, and (optionally) persists the result to S3.
- All activity is logged in a structured, CloudWatch-friendly format to support monitoring and incident response.

---

### Repository structure

- `serverless.yml` — Serverless Framework configuration for the API Gateway + Lambda stack.
- `workflow_lambda/handler.py` — Python 3.10 Lambda handler with validation, structured logging, and S3 persistence.
- `workflow_lambda/requirements.txt` — Python runtime dependencies (for local use and CI).
- `n8n-workflows/workflow.json` — Example n8n workflow that triggers the API Gateway webhook.
- `.github/workflows/deploy.yml` — GitHub Actions workflow for CI/CD and Serverless deployments.
- `tests/test_handler.py` — Pytest-based unit tests for the Lambda handler (with mocked S3 client).
- `architecture/architecture.md` — Architecture overview with Mermaid diagram.
- `runbook.md` — Operational runbook for common failure modes and rollback strategy.
- `.gitignore` — Standard Python/Node/Serverless ignores.

---

### Architecture overview

The platform is designed around a simple, robust event-driven pipeline:

- **n8n**: Entry point for external events and business logic orchestration.
- **API Gateway (POST /webhook)**: Public HTTPS endpoint managed by Serverless.
- **Lambda (`workflowHandler`)**:
  - Validates incoming JSON payloads.
  - Requires `required_field` to be present.
  - Attaches processing metadata (request ID, timestamps, etc.).
  - Logs events using structured, CloudWatch-friendly messages.
  - Persists results to S3 (when `S3_OUTPUT_BUCKET` is configured).
  - Is designed to be retry-safe and idempotent at the persistence layer.
- **S3**: Stores processed workflow outputs keyed by Lambda `request_id`.
- **CloudWatch**: Captures logs and metrics for ongoing monitoring and troubleshooting.

See `architecture/architecture.md` for the Mermaid diagram and more details.

---

### How the webhook works

1. **Inbound HTTP request**
   - An external system (or Power Automate / another workflow engine) calls the n8n webhook URL.

2. **n8n forwards payload**
   - The n8n workflow defined in `n8n-workflows/workflow.json` exposes a `POST` webhook.
   - The webhook node forwards the incoming JSON payload to your API Gateway endpoint:
     - `POST https://<your-api-gateway-id>.execute-api.<region>.amazonaws.com/prod/webhook`

3. **API Gateway → Lambda**
   - API Gateway forwards the request to the Lambda function `workflowHandler` defined in `serverless.yml`.
   - The Lambda:
     - Parses the request body as JSON.
     - Validates the presence of `required_field`.
     - Builds processing metadata (including a unique `request_id`).
     - Logs `workflow_received` and `workflow_processed` events.
     - Optionally writes an output document to S3.

4. **HTTP response**
   - On success, the Lambda returns:
     - HTTP `200`
     - JSON body with `status: "accepted"`, `request_id`, and `metadata`.
   - On validation failure (e.g., missing `required_field`), the Lambda returns:
     - HTTP `400`
     - JSON body with `error: "validation_failed"` and a descriptive message.
   - On unexpected internal errors, the Lambda returns:
     - HTTP `500`
     - JSON body with `error: "internal_error"`.

---

### Example webhook payload

When calling the API Gateway endpoint (directly or via n8n), the payload should be JSON and include `required_field`:

```json
{
  "required_field": "customer-123",
  "event_type": "order_created",
  "amount": 199.99,
  "currency": "USD",
  "metadata": {
    "source": "crm-system",
    "priority": "high"
  }
}
```

Example `curl` request against the deployed API Gateway endpoint:

```bash
curl -X POST "https://<your-api-gateway-id>.execute-api.<region>.amazonaws.com/prod/webhook" \
  -H "Content-Type: application/json" \
  -d '{
    "required_field": "customer-123",
    "event_type": "order_created",
    "amount": 199.99,
    "currency": "USD"
  }'
```

---

### n8n import instructions

1. Open your n8n instance.
2. Go to **Workflows → Import from file**.
3. Select `n8n-workflows/workflow.json` from this repository.
4. Open the imported workflow and update the **HTTP Request** node:
   - Set the URL to your deployed API Gateway endpoint:
     - `https://<your-api-gateway-id>.execute-api.<region>.amazonaws.com/prod/webhook`
   - Ensure the method is `POST`.
5. Activate the workflow.
6. Trigger the workflow (e.g., via the configured webhook) and verify that:
   - The Lambda receives the payload.
   - API Gateway returns HTTP `200`.
   - If configured, S3 contains a new object under `workflow-results/`.

---

### How to deploy (Serverless + GitHub Actions)

#### Prerequisites

- AWS account with permissions to:
  - Manage Lambda, API Gateway, CloudFormation, and IAM roles.
  - Put objects into the target S3 bucket.
- A GitHub repository with this project.
- A pre-created S3 bucket to store workflow results (optional but recommended).

#### Required GitHub secrets

Configure the following GitHub Actions secrets in your repository:

- `AWS_ACCESS_KEY_ID` — IAM user or role access key with deploy permissions.
- `AWS_SECRET_ACCESS_KEY` — IAM secret key.
- `AWS_REGION` — AWS region to deploy to (e.g., `us-east-1`).
- `S3_OUTPUT_BUCKET` — Name of the S3 bucket for Lambda output (must exist).

#### CI/CD workflow

The GitHub Actions workflow in `.github/workflows/deploy.yml`:

- Runs on:
  - `push` to `feat/workflow-automation`.
  - `pull_request` targeting `main`.
- Steps:
  - Checks out the repository.
  - Installs Node.js and the Serverless Framework CLI.
  - Sets up Python 3.10 and installs `workflow_lambda/requirements.txt` plus `pytest`.
  - Runs `pytest` to execute unit tests.
  - Configures AWS credentials from GitHub secrets.
  - Deploys the stack via `serverless deploy --stage prod`.

You can also deploy locally from your machine:

```bash
export AWS_REGION=us-east-1
export S3_OUTPUT_BUCKET=your-output-bucket-name

pip install -r workflow_lambda/requirements.txt
npm install -g serverless

serverless deploy --stage prod
```

---

### Monitoring and observability

The Lambda handler uses structured, CloudWatch-friendly logging:

- Logs include:
  - `event` (e.g., `workflow_received`, `workflow_processed`, `validation_failed`, `s3_persist_success`, `s3_persist_failure`, `workflow_error`).
  - `request_id` for traceability across log entries.
  - Additional contextual fields such as `bucket`, `key`, and `required_field`.
- Use CloudWatch Logs to:
  - Filter by `request_id` to follow a single invocation end-to-end.
  - Filter by `event` to identify patterns or spikes in validation or persistence failures.

You can extend this with CloudWatch metrics and alarms (for example, using Metric Filters on specific `event` values).

---

### Runbook

Operational guidance (including handling validation failures, Lambda failures, IAM issues, retry strategy, and rollback steps) is documented in:

- `runbook.md`

Use that document as the starting point during incidents and when refining operational procedures.

