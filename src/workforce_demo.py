"""Create idempotent synthetic records for the workforce portfolio walkthrough."""
from datetime import date, timedelta

from src import db


def seed() -> None:
    db._ensure_tables()
    session = db.SessionLocal()
    try:
        if session.get(db.TenantRecord, "demo") is None:
            session.add(db.TenantRecord(key="demo", name="FlowOps Demo Tenant", active=True))
            session.flush()
        employee = session.query(db.EmployeeRecord).filter_by(employee_number="DEMO-1001").one_or_none()
        if employee is None:
            employee = db.EmployeeRecord(
                employee_number="DEMO-1001", full_name="Avery Santos",
                work_email="avery.santos@example.test", department="Finance",
                job_title="Operations Analyst", start_date=(date.today() + timedelta(days=30)).isoformat(),
                manager_subject="manager-demo", tenant_key="demo", status="ACTIVE")
            session.add(employee)
            session.flush()
            session.add(db.StaffOnboardingRecord(employee_id=employee.id, tenant_key="demo",
                created_by="demo-seed", status="DRAFT"))
            session.add(db.WorkforceAuditRecord(actor_subject="demo-seed", action="demo.employee_seeded",
                tenant_key="demo", entity_type="employee", entity_id=str(employee.id),
                detail="Synthetic portfolio demo record."))
        asset = session.query(db.ITAssetRecord).filter_by(asset_tag="DEMO-LAPTOP-001").one_or_none()
        if asset is None:
            session.add(db.ITAssetRecord(asset_tag="DEMO-LAPTOP-001", serial_number="DEMO-SN-0001",
                category="Laptop", brand_model="Example Business 14", condition="GOOD", status="AVAILABLE",
                tenant_key="demo"))
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


if __name__ == "__main__":
    seed()
    print("Synthetic workforce demo records are ready.")
