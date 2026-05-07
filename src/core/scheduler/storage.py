"""Storage backends for scheduler jobs.

This module provides abstraction for scheduler job persistence with multiple
backend implementations (JSON files, Firestore, etc.).
"""

import json
import logging
from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class JobStorage(ABC):
    """Abstract base class for job storage backends."""

    @abstractmethod
    async def load_jobs(self) -> list[dict[str, Any]]:
        """Load all jobs from storage.
        
        Returns:
            List of job dicts
        """
        pass

    @abstractmethod
    async def save_jobs(self, jobs: list[dict[str, Any]]) -> None:
        """Save all jobs to storage.
        
        Args:
            jobs: List of job dicts to save
        """
        pass

    async def save_job(self, job: dict[str, Any]) -> None:
        """Create or update a single job without replacing unrelated jobs."""
        job_id = job["id"]
        jobs = await self.load_jobs()
        for index, existing in enumerate(jobs):
            if existing.get("id") == job_id:
                jobs[index] = job
                break
        else:
            jobs.append(job)
        await self.save_jobs(jobs)

    @abstractmethod
    async def claim_job(self, job_id: str, lease_duration_seconds: int = 300) -> bool:
        """Attempt to claim a job for execution with a lease.
        
        This is used for distributed locking to prevent duplicate execution.
        
        Args:
            job_id: Job ID to claim
            lease_duration_seconds: How long to hold the lease (default 5 minutes)
            
        Returns:
            True if claim succeeded, False if already claimed by another worker
        """
        pass

    @abstractmethod
    async def release_job(self, job_id: str) -> None:
        """Release a job lease.
        
        Args:
            job_id: Job ID to release
        """
        pass


class JSONFileStorage(JobStorage):
    """JSON file-based storage (legacy/dev mode).
    
    Simple file-based storage for backward compatibility and local development.
    Not suitable for production with multiple Cloud Run instances (no distributed locking).
    """

    def __init__(self, memory_dir: Path):
        """Initialize JSON file storage.
        
        Args:
            memory_dir: Directory for storing scheduler data
        """
        self.memory_dir = memory_dir
        self.jobs_file = memory_dir / "scheduler" / "jobs.json"

    async def load_jobs(self) -> list[dict[str, Any]]:
        """Load jobs from JSON file."""
        if self.jobs_file.exists():
            jobs = json.loads(self.jobs_file.read_text())
            logger.info(f"Loaded {len(jobs)} jobs from JSON file")
            return jobs
        return []

    async def save_jobs(self, jobs: list[dict[str, Any]]) -> None:
        """Save jobs to JSON file."""
        self.jobs_file.parent.mkdir(parents=True, exist_ok=True)
        self.jobs_file.write_text(json.dumps(jobs, indent=2))

    async def save_job(self, job: dict[str, Any]) -> None:
        """Create or replace one JSON-backed job."""
        job_id = job["id"]
        jobs = await self.load_jobs()
        for index, existing in enumerate(jobs):
            if existing.get("id") == job_id:
                jobs[index] = job
                break
        else:
            jobs.append(job)
        await self.save_jobs(jobs)

    async def claim_job(self, job_id: str, lease_duration_seconds: int = 300) -> bool:
        """Claim job (no-op for JSON storage - no distributed locking)."""
        # JSON file storage doesn't support distributed locking
        # Always return True for backward compatibility
        logger.warning(
            f"JSON storage does not support distributed locking for job {job_id}"
        )
        return True

    async def release_job(self, job_id: str) -> None:
        """Release job (no-op for JSON storage)."""
        pass


class FirestoreStorage(JobStorage):
    """Firestore-based storage for production use.
    
    Provides distributed locking via conditional updates for safe concurrent
    execution across multiple Cloud Run instances.
    """

    def __init__(self, project_id: str, collection_name: str = "scheduler_jobs"):
        """Initialize Firestore storage.
        
        Args:
            project_id: GCP project ID
            collection_name: Firestore collection name for jobs
        """
        from google.cloud import firestore
        
        self.db = firestore.Client(project=project_id)
        self.collection = self.db.collection(collection_name)
        logger.info(f"Initialized Firestore storage: {collection_name}")

    async def load_jobs(self) -> list[dict[str, Any]]:
        """Load all jobs from Firestore."""
        jobs = []
        docs = self.collection.stream()
        
        for doc in docs:
            job_data = doc.to_dict()
            job_data["id"] = doc.id  # Ensure ID is set
            jobs.append(job_data)
        
        logger.info(f"Loaded {len(jobs)} jobs from Firestore")
        return jobs

    async def save_jobs(self, jobs: list[dict[str, Any]]) -> None:
        """Save jobs to Firestore.
        
        Note: This is a bulk operation that creates/updates documents.
        For production, prefer individual job updates to avoid conflicts.
        """
        batch = self.db.batch()
        
        for job in jobs:
            job_id = job["id"]
            doc_ref = self.collection.document(job_id)
            
            # Create a copy without internal fields
            job_data = {k: v for k, v in job.items() if not k.startswith("_")}
            batch.set(doc_ref, job_data, merge=True)
        
        batch.commit()
        logger.info(f"Saved {len(jobs)} jobs to Firestore")

    async def save_job(self, job: dict[str, Any]) -> None:
        """Merge one job document without touching lease ownership fields."""
        job_id = job["id"]
        doc_ref = self.collection.document(job_id)
        job_data = {
            k: v
            for k, v in job.items()
            if not k.startswith("_") and k not in {"lease_until", "lease_claimed_at"}
        }
        doc_ref.set(job_data, merge=True)
        logger.info(f"Saved job {job_id} to Firestore")

    async def claim_job(self, job_id: str, lease_duration_seconds: int = 300) -> bool:
        """Attempt to claim a job with distributed locking.
        
        Uses Firestore transactions for atomic claim with conditional update.
        
        Args:
            job_id: Job ID to claim
            lease_duration_seconds: Lease duration (default 5 minutes)
            
        Returns:
            True if claim succeeded, False if already claimed
        """
        from google.cloud import firestore
        
        doc_ref = self.collection.document(job_id)
        transaction = self.db.transaction()
        
        try:
            @firestore.transactional
            def claim_in_transaction(transaction, doc_ref):
                snapshot = doc_ref.get(transaction=transaction)
                
                if not snapshot.exists:
                    logger.warning(f"Job {job_id} not found in Firestore")
                    return False
                
                job_data = snapshot.to_dict()
                now = datetime.now(timezone.utc)
                
                # Check if job has an active lease
                lease_until = job_data.get("lease_until")
                if lease_until:
                    lease_expiry = datetime.fromisoformat(lease_until)
                    # Ensure timezone-aware for comparison
                    if lease_expiry.tzinfo is None:
                        lease_expiry = lease_expiry.replace(tzinfo=timezone.utc)
                        logger.warning(
                            f"Job {job_id} had timezone-naive lease_until, assuming UTC"
                        )
                    if lease_expiry > now:
                        logger.info(
                            f"Job {job_id} already claimed until {lease_until}"
                        )
                        return False
                
                # Claim the job
                lease_expiry = now + timedelta(seconds=lease_duration_seconds)
                transaction.update(doc_ref, {
                    "lease_until": lease_expiry.isoformat(),
                    "lease_claimed_at": now.isoformat(),
                })
                
                logger.info(f"Claimed job {job_id} with lease until {lease_expiry}")
                return True
            
            return claim_in_transaction(transaction, doc_ref)
            
        except Exception as e:
            logger.error(f"Failed to claim job {job_id}: {e}")
            return False

    async def release_job(self, job_id: str) -> None:
        """Release a job lease."""
        from google.cloud.firestore import DELETE_FIELD
        
        doc_ref = self.collection.document(job_id)
        
        try:
            doc_ref.update({
                "lease_until": DELETE_FIELD,
                "lease_claimed_at": DELETE_FIELD,
            })
            logger.info(f"Released lease for job {job_id}")
        except Exception as e:
            logger.error(f"Failed to release job {job_id}: {e}")


def create_storage(
    storage_type: str,
    memory_dir: Path | None = None,
    project_id: str | None = None,
) -> JobStorage:
    """Factory function to create appropriate storage backend.
    
    Args:
        storage_type: "json" or "firestore"
        memory_dir: Directory for JSON storage (required if storage_type="json")
        project_id: GCP project ID (required if storage_type="firestore")
        
    Returns:
        JobStorage instance
        
    Raises:
        ValueError: If required parameters are missing
        
    Example:
        >>> # JSON storage for dev
        >>> storage = create_storage("json", memory_dir=Path("./data/memory"))
        >>> 
        >>> # Firestore for production
        >>> storage = create_storage("firestore", project_id="my-project")
    """
    if storage_type == "json":
        if not memory_dir:
            # Use temp directory if memory_dir not provided
            import tempfile
            memory_dir = Path(tempfile.gettempdir()) / "monkey_bot_scheduler"
        return JSONFileStorage(memory_dir)
    
    elif storage_type == "firestore":
        if not project_id:
            raise ValueError("project_id required for Firestore storage")
        return FirestoreStorage(project_id)
    
    else:
        raise ValueError(f"Unknown storage type: {storage_type}")
