"""Unit tests for scheduler storage backends."""

from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from src.core.scheduler.storage import (
    FirestoreStorage,
    JSONFileStorage,
    create_storage,
)


class TestJSONFileStorage:
    """Test JSON file storage backend."""
    
    @pytest.fixture
    def storage(self, tmp_path):
        """Create JSON file storage instance."""
        return JSONFileStorage(tmp_path)
    
    @pytest.mark.asyncio
    async def test_load_empty_jobs(self, storage):
        """Test loading from non-existent file returns empty list."""
        jobs = await storage.load_jobs()
        assert jobs == []
    
    @pytest.mark.asyncio
    async def test_save_and_load_jobs(self, storage):
        """Test saving and loading jobs."""
        test_jobs = [
            {
                "id": "job1",
                "job_type": "test",
                "status": "pending",
                "schedule_at": datetime.utcnow().isoformat(),
            },
            {
                "id": "job2",
                "job_type": "test",
                "status": "completed",
                "schedule_at": datetime.utcnow().isoformat(),
            },
        ]
        
        await storage.save_jobs(test_jobs)
        loaded_jobs = await storage.load_jobs()
        
        assert len(loaded_jobs) == 2
        assert loaded_jobs[0]["id"] == "job1"
        assert loaded_jobs[1]["id"] == "job2"

    @pytest.mark.asyncio
    async def test_save_job_updates_one_job(self, storage):
        """Test saving one job preserves unrelated jobs."""
        await storage.save_jobs([
            {"id": "job1", "status": "pending"},
            {"id": "job2", "status": "pending"},
        ])

        await storage.save_job({"id": "job1", "status": "completed"})
        loaded_jobs = await storage.load_jobs()
        by_id = {job["id"]: job for job in loaded_jobs}

        assert by_id["job1"]["status"] == "completed"
        assert by_id["job2"]["status"] == "pending"
    
    @pytest.mark.asyncio
    async def test_claim_job_always_succeeds(self, storage):
        """Test that JSON storage always claims successfully (no locking)."""
        # JSON storage doesn't support locking
        claimed = await storage.claim_job("job1")
        assert claimed is True
        
        # Second claim also succeeds (no locking)
        claimed_again = await storage.claim_job("job1")
        assert claimed_again is True
    
    @pytest.mark.asyncio
    async def test_release_job_is_noop(self, storage):
        """Test that release is a no-op for JSON storage."""
        # Should not raise any errors
        await storage.release_job("job1")
    
    def test_creates_directory(self, storage, tmp_path):
        """Test that save creates scheduler directory."""
        jobs = [{"id": "test"}]
        
        import asyncio
        asyncio.run(storage.save_jobs(jobs))
        
        scheduler_dir = tmp_path / "scheduler"
        assert scheduler_dir.exists()
        assert scheduler_dir.is_dir()


class TestFirestoreStorage:
    """Test Firestore storage backend."""
    
    @pytest.fixture
    def mock_firestore(self):
        """Create mock Firestore client."""
        with patch('google.cloud.firestore.Client') as mock:
            mock_client = MagicMock()
            mock.return_value = mock_client
            yield mock_client
    
    @pytest.fixture
    def storage(self, mock_firestore):
        """Create Firestore storage instance with mocked client."""
        with patch('google.cloud.firestore.Client', return_value=mock_firestore):
            return FirestoreStorage(project_id="test-project")
    
    @pytest.mark.asyncio
    async def test_load_jobs_from_firestore(self, storage, mock_firestore):
        """Test loading jobs from Firestore."""
        # Mock Firestore documents
        mock_doc1 = MagicMock()
        mock_doc1.id = "job1"
        mock_doc1.to_dict.return_value = {
            "job_type": "test",
            "status": "pending",
        }
        
        mock_doc2 = MagicMock()
        mock_doc2.id = "job2"
        mock_doc2.to_dict.return_value = {
            "job_type": "test",
            "status": "completed",
        }
        
        mock_firestore.collection.return_value.stream.return_value = [
            mock_doc1,
            mock_doc2,
        ]
        
        jobs = await storage.load_jobs()
        
        assert len(jobs) == 2
        assert jobs[0]["id"] == "job1"
        assert jobs[1]["id"] == "job2"
    
    @pytest.mark.asyncio
    async def test_save_jobs_to_firestore(self, storage, mock_firestore):
        """Test saving jobs to Firestore."""
        test_jobs = [
            {"id": "job1", "status": "pending"},
            {"id": "job2", "status": "completed"},
        ]
        
        mock_batch = MagicMock()
        mock_firestore.batch.return_value = mock_batch
        
        await storage.save_jobs(test_jobs)
        
        # Should create batch and commit
        mock_firestore.batch.assert_called_once()
        mock_batch.commit.assert_called_once()

    @pytest.mark.asyncio
    async def test_save_job_to_firestore_preserves_lease_fields(self, storage, mock_firestore):
        """Single-job saves should not overwrite lease fields owned by claim/release."""
        mock_doc_ref = MagicMock()
        mock_firestore.collection.return_value.document.return_value = mock_doc_ref

        await storage.save_job({
            "id": "job1",
            "status": "running",
            "lease_until": "stale",
            "lease_claimed_at": "stale",
        })

        mock_firestore.collection.return_value.document.assert_called_once_with("job1")
        mock_doc_ref.set.assert_called_once()
        saved_payload = mock_doc_ref.set.call_args.args[0]
        assert saved_payload == {"id": "job1", "status": "running"}
        assert mock_doc_ref.set.call_args.kwargs == {"merge": True}
    
    @pytest.mark.asyncio
    async def test_claim_job_success(self, storage, mock_firestore):
        """Test successfully claiming an unclaimed job."""
        # Mock document without active lease
        mock_snapshot = MagicMock()
        mock_snapshot.exists = True
        mock_snapshot.to_dict.return_value = {
            "id": "job1",
            "status": "pending",
            # No lease_until field
        }
        
        mock_doc_ref = MagicMock()
        mock_doc_ref.get.return_value = mock_snapshot
        
        mock_firestore.collection.return_value.document.return_value = mock_doc_ref
        
        # Mock transaction
        mock_transaction = MagicMock()
        mock_firestore.transaction.return_value = mock_transaction
        
        claimed = await storage.claim_job("job1")
        
        # Should succeed
        assert claimed is True
    
    @pytest.mark.asyncio
    async def test_claim_job_already_claimed(self, storage, mock_firestore):
        """Test claiming a job that's already claimed."""
        # Mock document with active lease
        future_time = datetime.utcnow() + timedelta(minutes=5)
        
        mock_snapshot = MagicMock()
        mock_snapshot.exists = True
        mock_snapshot.to_dict.return_value = {
            "id": "job1",
            "status": "pending",
            "lease_until": future_time.isoformat(),
        }
        
        mock_doc_ref = MagicMock()
        mock_doc_ref.get.return_value = mock_snapshot
        
        mock_firestore.collection.return_value.document.return_value = mock_doc_ref
        
        # Mock transaction
        mock_transaction = MagicMock()
        mock_firestore.transaction.return_value = mock_transaction
        
        claimed = await storage.claim_job("job1")
        
        # Should fail (already claimed)
        assert claimed is False
    
    @pytest.mark.asyncio
    async def test_claim_job_expired_lease(self, storage, mock_firestore):
        """Test claiming a job with expired lease."""
        # Mock document with expired lease
        past_time = datetime.utcnow() - timedelta(minutes=5)
        
        mock_snapshot = MagicMock()
        mock_snapshot.exists = True
        mock_snapshot.to_dict.return_value = {
            "id": "job1",
            "status": "pending",
            "lease_until": past_time.isoformat(),
        }
        
        mock_doc_ref = MagicMock()
        mock_doc_ref.get.return_value = mock_snapshot
        
        mock_firestore.collection.return_value.document.return_value = mock_doc_ref
        
        # Mock transaction
        mock_transaction = MagicMock()
        mock_firestore.transaction.return_value = mock_transaction
        
        claimed = await storage.claim_job("job1")
        
        # Should succeed (lease expired)
        assert claimed is True
    
    @pytest.mark.asyncio
    async def test_release_job(self, storage, mock_firestore):
        """Test releasing a job lease."""
        mock_doc_ref = MagicMock()
        mock_firestore.collection.return_value.document.return_value = mock_doc_ref
        
        await storage.release_job("job1")
        
        # Should update document to remove lease fields
        mock_doc_ref.update.assert_called_once()


class TestCreateStorage:
    """Test storage factory function."""
    
    def test_create_json_storage(self, tmp_path):
        """Test creating JSON storage."""
        storage = create_storage("json", memory_dir=tmp_path)
        assert isinstance(storage, JSONFileStorage)
    
    def test_create_firestore_storage(self):
        """Test creating Firestore storage."""
        with patch('google.cloud.firestore.Client'):
            storage = create_storage("firestore", project_id="test-project")
            assert isinstance(storage, FirestoreStorage)
    
    def test_create_json_without_memory_dir(self):
        """Test that JSON storage uses temp dir when memory_dir is None."""
        storage = create_storage("json")
        assert isinstance(storage, JSONFileStorage)
        # Should have created storage with a temp directory
        assert storage.memory_dir is not None
        assert "monkey_bot_scheduler" in str(storage.memory_dir)
    
    def test_create_firestore_without_project_id(self):
        """Test that Firestore storage requires project_id."""
        with pytest.raises(ValueError, match="project_id required"):
            create_storage("firestore")
    
    def test_create_unknown_storage_type(self):
        """Test that unknown storage type raises error."""
        with pytest.raises(ValueError, match="Unknown storage type"):
            create_storage("unknown")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
