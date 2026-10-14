# Employee Attendance & Analytics API

A FastAPI backend for employee attendance tracking and reporting, backed by MongoDB. Implements CRUD, business rules (R1-R10), concurrency-safe punch/regularization operations, and high-performance indexed aggregation pipelines.

## Requirements

- Python 3.11+
- MongoDB 6.0+ (Local or MongoDB Atlas)

## Setup & Running

1. Install dependencies:
```bash
pip install -r requirements.txt
```

2. Configure environment variables (or provide via `.env`):
```bash
export MONGO_URI="mongodb://localhost:27017"
export MONGO_DB="attendance_db"
```

3. (Optional) Load sample documents:
```bash
python sample_seed.py
```

4. Start the application:
```bash
uvicorn app.main:app --port 8000
```

All indexes are automatically created on startup. The service is ready when `GET /health` returns HTTP 200 `{"status": "ok"}`.

## API Documentation

Interactive Swagger documentation is available at `http://localhost:8000/docs`. The complete API contract is specified in `openapi.yaml`.

## Key Files

- `app/main.py`: Complete application code including models, endpoints, helpers, and index initialization.
- `REVIEW.md`: Comprehensive audit of defects identified in the starter implementation and their resolutions.
- `DECISIONS.md`: Explanations of indexing strategy, concurrency handling, tie-breaking, headcount calculation, and 100x scalability architecture.
