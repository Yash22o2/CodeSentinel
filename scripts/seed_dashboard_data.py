import os
import sys
from datetime import datetime, timedelta
import random

# Add project root to sys.path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlmodel import Session, select
from app.db.session import engine, create_db_and_tables
from app.db.models import ReviewMetric

def seed_data():
    print("Creating tables if they don't exist...")
    create_db_and_tables()

    with Session(engine) as session:
        # Check if already seeded to prevent duplicate data
        existing = session.exec(select(ReviewMetric)).first()
        if existing:
            print("Database already contains data. Skipping seed.")
            return

        print("Seeding dummy data for the last 30 days...")
        now = datetime.utcnow()
        metrics = []

        for i in range(30):
            # Generate 1 to 5 PRs per day
            num_prs = random.randint(1, 5)
            date = now - timedelta(days=30 - i)
            
            for _ in range(num_prs):
                total_latency_ms = random.randint(2000, 15000)
                total_tokens = random.randint(1000, 10000)
                estimated_cost_usd = total_tokens * 0.00002 # random cost logic
                
                # Findings
                security = random.randint(0, 3)
                style = random.randint(0, 10)
                logic = random.randint(0, 5)
                test = random.randint(0, 4)

                metric = ReviewMetric(
                    pr_id=f"dummy-pr-{random.randint(1000, 9999)}",
                    total_latency_ms=total_latency_ms,
                    total_tokens=total_tokens,
                    estimated_cost_usd=estimated_cost_usd,
                    created_at=date,
                    security_findings_kept=security,
                    style_findings_kept=style,
                    logic_findings_kept=logic,
                    test_findings_kept=test
                )
                metrics.append(metric)

        session.add_all(metrics)
        session.commit()
        print(f"Successfully seeded {len(metrics)} ReviewMetric records.")

if __name__ == "__main__":
    seed_data()
