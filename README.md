# Employee Attendance & Analytics API

A FastAPI and MongoDB backend service for managing employee attendance, punch records, regularization workflows, and analytics with MongoDB aggregation pipelines.

## Prerequisites

- Python 3.11+
- MongoDB 6.0+ (Atlas or local instance)

## Setup & Running

1. **Create and activate a virtual environment:**

   ```bash
   python -m venv .venv
   source .venv/bin/activate  # On Windows: .venv\Scripts\activate
   ```

2. **Install dependencies:**

   ```bash
   pip install -r requirements.txt
   ```

3. **Configure environment variables:**
   Export your MongoDB connection URI and database name (or define in a local `.env`):

   ```bash
   export MONGO_URI="mongodb://localhost:27017"
   export MONGO_DB="Attendance"
   ```

4. **Seed sample data (optional):**

   ```bash
   python sample_seed.py
   ```

5. **Start the API server:**

   ```bash
   uvicorn app.main:app --port 8000
   ```

   The application creates all required database indexes automatically during startup. The interactive OpenAPI documentation is available at `http://localhost:8000/docs`.

6. **Run tests:**
   ```bash
   pytest
   ```

## Repository Structure

- `app/main.py`: Complete implementation of all 12 endpoints, validations, business rules (R1–R10), and aggregation pipelines.
- `requirements.txt`: Minimal, pinned Python dependencies.
- `REVIEW.md`: Part A code review detailing defects identified and resolved from the starter code.
- `DECISIONS.md`: Part C architectural decisions covering indexing strategies, concurrency handling, tie-breaking, and scalability.
