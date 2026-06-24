# Implementation Patterns & Reference Examples

**Purpose:** Detailed code examples for common implementation patterns, testing strategies, error handling, and troubleshooting. Referenced by the `implementer` subagent during Phase 4 implementation.

---

## Table of Contents

1. [Testing Patterns](#testing-patterns)
2. [Error Handling Patterns](#error-handling-patterns)
3. [Logging Best Practices](#logging-best-practices)
4. [Common Architecture Patterns](#common-architecture-patterns)
5. [Anti-Patterns to Avoid](#anti-patterns-to-avoid)
6. [Troubleshooting](#troubleshooting)

---

## Testing Patterns

### Unit Test Structure

```python
import pytest
from unittest.mock import Mock, AsyncMock
from uuid import uuid4
 
class TestFavoritesService:
    """Test suite for FavoritesService."""
    
    @pytest.fixture
    def mock_repository(self):
        """Create mock repository for testing."""
        repo = Mock(spec=FavoritesRepository)
        repo.add = AsyncMock()
        repo.find_by_user = AsyncMock()
        return repo
    
    @pytest.fixture
    def service(self, mock_repository):
        """Create service with mocked dependencies."""
        return FavoritesService(repository=mock_repository)
    
    async def test_add_favorite_success(self, service, mock_repository):
        """Test adding favorite with valid input."""
        # Arrange
        user_id = uuid4()
        product_id = uuid4()
        expected = Favorite(id=uuid4(), user_id=user_id, product_id=product_id)
        mock_repository.add.return_value = expected
        
        # Act
        result = await service.add_favorite(user_id, product_id)
        
        # Assert
        assert result == expected
        mock_repository.add.assert_called_once_with(user_id, product_id)
    
    async def test_add_favorite_raises_conflict_error(self, service, mock_repository):
        """Test that ConflictError is raised when favorite already exists."""
        # Arrange
        mock_repository.add.side_effect = ConflictError("Already favorited")
        
        # Act & Assert
        with pytest.raises(ConflictError, match="Already favorited"):
            await service.add_favorite(uuid4(), uuid4())
```

### What to Test

**Always test:**
- Happy path (normal operation)
- Error cases from code spec
- Edge cases (empty inputs, boundary values, null/undefined)
- Input validation

**Don't test:**
- Framework code (trust the framework)
- Simple getters/setters without logic
- Third-party library internals

### Test Data Factories

```python
from dataclasses import replace
from datetime import datetime
from uuid import uuid4
 
def create_test_user(**overrides) -> User:
    """Factory function for creating test users.
    
    Args:
        **overrides: Fields to override in the default user
        
    Returns:
        User instance with test data
    """
    defaults = {
        'id': uuid4(),
        'email': 'test@example.com',
        'name': 'Test User',
        'created_at': datetime(2024, 1, 1),
    }
    return User(**{**defaults, **overrides})
 
# Usage
user = create_test_user(email='custom@example.com')
```

### Mocking Strategy

```python
from unittest.mock import Mock, AsyncMock
 
# Mock external dependencies
mock_repository = Mock(spec=FavoritesRepository)
mock_repository.find_by_id = AsyncMock()
mock_repository.save = AsyncMock()
mock_repository.delete = AsyncMock()
 
# Don't mock the unit under test
service = FavoritesService(repository=mock_repository)
 
# Mock return values
mock_repository.find_by_id.return_value = test_favorite
 
# Verify calls
mock_repository.find_by_id.assert_called_once_with(user_id)
assert mock_repository.find_by_id.call_count == 1
```

### Integration Test Structure

```python
import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession
 
class TestFavoritesAPIIntegration:
    """Integration tests for Favorites API."""
    
    @pytest.fixture
    async def client(self, app):
        """Create test client."""
        async with AsyncClient(app=app, base_url="http://test") as client:
            yield client
    
    @pytest.fixture
    async def auth_token(self, client):
        """Get authentication token for tests."""
        response = await client.post("/auth/login", json={
            "email": "test@example.com",
            "password": "testpass123"
        })
        return response.json()["access_token"]
    
    @pytest.fixture(autouse=True)
    async def clean_database(self, db_session: AsyncSession):
        """Clean database before each test."""
        await db_session.execute("DELETE FROM favorites")
        await db_session.commit()
        yield
        await db_session.execute("DELETE FROM favorites")
        await db_session.commit()
    
    async def test_add_favorite_and_retrieve(self, client, auth_token):
        """Test adding favorite and retrieving it."""
        # Add favorite
        add_response = await client.post(
            "/favorites",
            headers={"Authorization": f"Bearer {auth_token}"},
            json={"product_id": "550e8400-e29b-41d4-a716-446655440000"}
        )
        assert add_response.status_code == 201
        assert "id" in add_response.json()
        
        # Retrieve favorites
        get_response = await client.get(
            "/favorites",
            headers={"Authorization": f"Bearer {auth_token}"}
        )
        assert get_response.status_code == 200
        favorites = get_response.json()["items"]
        assert len(favorites) == 1
        assert favorites[0]["product_id"] == "550e8400-e29b-41d4-a716-446655440000"
```

---

## Error Handling Patterns

### Custom Error Classes

```python
class NotFoundError(Exception):
    """Raised when an entity is not found."""
    
    def __init__(self, entity: str, entity_id: str):
        self.entity = entity
        self.entity_id = entity_id
        super().__init__(f"{entity} with id {entity_id} not found")
 
# Usage
if not product:
    raise NotFoundError('Product', str(product_id))
```

### Try-Except at Boundary

```python
# Service raises
async def add_favorite(self, user_id: UUID, product_id: UUID) -> Favorite:
    """Add favorite with validation."""
    product = await self.products_service.find_by_id(product_id)
    if not product:
        raise NotFoundError('Product', str(product_id))
    return await self.repository.add(user_id, product_id)
 
# Controller/Route catches
@router.post("/favorites")
async def create_favorite(dto: CreateFavoriteDto, user: User = Depends(get_current_user)):
    """Create a new favorite."""
    try:
        return await service.add_favorite(user.id, dto.product_id)
    except NotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
```

---

## Logging Best Practices

```python
import logging
import json
from datetime import datetime
 
logger = logging.getLogger(__name__)
 
# Good: Structured logging with context
logger.info(
    json.dumps({
        "message": "Adding favorite",
        "user_id": str(user_id),
        "product_id": str(product_id),
        "request_id": context.request_id,
        "timestamp": datetime.utcnow().isoformat(),
    })
)
 
# Good: Log errors with stack traces
logger.error(
    json.dumps({
        "message": "Failed to add favorite",
        "user_id": str(user_id),
        "product_id": str(product_id),
        "error": str(error),
        "timestamp": datetime.utcnow().isoformat(),
    }),
    exc_info=True
)
 
# Bad: Unstructured logging
print('adding favorite')
 
# Bad: Logging sensitive data
logger.info(f"User login: {user.password}")  # NEVER DO THIS
```

---

## Common Architecture Patterns

### Repository Pattern

```python
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import IntegrityError
from uuid import UUID
 
class FavoritesRepository:
    """Repository for favorites data access."""
    
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
    
    async def add(self, user_id: UUID, product_id: UUID) -> Favorite:
        """Add favorite to database.
        
        Args:
            user_id: User UUID
            product_id: Product UUID
            
        Returns:
            Created favorite
            
        Raises:
            ConflictError: If favorite already exists
        """
        favorite = FavoriteModel(user_id=user_id, product_id=product_id)
        self.session.add(favorite)
        
        try:
            await self.session.commit()
            await self.session.refresh(favorite)
            return Favorite.from_orm(favorite)
        except IntegrityError:
            await self.session.rollback()
            raise ConflictError("Favorite already exists")
    
    async def find_by_user(
        self,
        user_id: UUID,
        pagination: PaginationParams
    ) -> list[Favorite]:
        """Get favorites for user with pagination.
        
        Args:
            user_id: User UUID
            pagination: Offset and limit
            
        Returns:
            List of favorites
        """
        result = await self.session.execute(
            select(FavoriteModel)
            .where(FavoriteModel.user_id == user_id)
            .offset(pagination.offset)
            .limit(pagination.limit)
            .order_by(FavoriteModel.created_at.desc())
        )
        return [Favorite.from_orm(f) for f in result.scalars().all()]
```

### Service Pattern

```python
from uuid import UUID
 
class FavoritesService:
    """Service for favorites business logic."""
    
    def __init__(
        self,
        repository: FavoritesRepository,
        products_service: ProductsService,
        event_emitter: EventEmitter,
    ) -> None:
        self.repository = repository
        self.products_service = products_service
        self.event_emitter = event_emitter
    
    async def add_favorite(self, user_id: UUID, product_id: UUID) -> FavoriteDto:
        """Add favorite with validation.
        
        Args:
            user_id: User UUID
            product_id: Product UUID
            
        Returns:
            Created favorite DTO
            
        Raises:
            NotFoundError: If product doesn't exist
            ConflictError: If already favorited
        """
        # Validate product exists
        product = await self.products_service.find_by_id(product_id)
        if not product:
            raise NotFoundError('Product', str(product_id))
        
        # Add favorite
        favorite = await self.repository.add(user_id, product_id)
        
        # Emit event
        await self.event_emitter.emit('favorite.added', {
            'user_id': str(user_id),
            'product_id': str(product_id),
            'timestamp': datetime.utcnow().isoformat(),
        })
        
        return self._to_dto(favorite)
    
    def _to_dto(self, favorite: Favorite) -> FavoriteDto:
        """Convert entity to DTO."""
        return FavoriteDto(
            id=favorite.id,
            user_id=favorite.user_id,
            product_id=favorite.product_id,
            created_at=favorite.created_at.isoformat(),
        )
```

### Controller/Router Pattern (FastAPI)

```python
from fastapi import APIRouter, Depends, HTTPException, status
from uuid import UUID
 
router = APIRouter(prefix="/favorites", tags=["favorites"])
 
@router.post("/", status_code=status.HTTP_201_CREATED, response_model=FavoriteResponse)
async def add_favorite(
    dto: AddFavoriteDto,
    user: User = Depends(get_current_user),
    service: FavoritesService = Depends(get_favorites_service),
) -> FavoriteResponse:
    """Add a product to user's favorites.
    
    Args:
        dto: Request body with product_id
        user: Current authenticated user
        service: Favorites service instance
        
    Returns:
        Created favorite
        
    Raises:
        HTTPException: 404 if product not found, 409 if already favorited
    """
    try:
        return await service.add_favorite(user.id, dto.product_id)
    except NotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ConflictError as e:
        raise HTTPException(status_code=409, detail=str(e))
 
@router.delete("/{product_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_favorite(
    product_id: UUID,
    user: User = Depends(get_current_user),
    service: FavoritesService = Depends(get_favorites_service),
) -> None:
    """Remove a product from user's favorites."""
    await service.remove_favorite(user.id, product_id)
 
@router.get("/", response_model=PaginatedResponse[FavoriteResponse])
async def get_favorites(
    pagination: PaginationParams = Depends(),
    user: User = Depends(get_current_user),
    service: FavoritesService = Depends(get_favorites_service),
) -> PaginatedResponse[FavoriteResponse]:
    """Get user's favorites with pagination."""
    items = await service.get_user_favorites(user.id, pagination)
    total = await service.count_user_favorites(user.id)
    
    return PaginatedResponse(
        items=items,
        total=total,
        offset=pagination.offset,
        limit=pagination.limit,
        has_more=pagination.offset + len(items) < total,
    )
```

### DTO Validation (Pydantic)

```python
from pydantic import BaseModel, Field
from uuid import UUID
 
class AddFavoriteDto(BaseModel):
    """Request body for adding a favorite."""
    product_id: UUID = Field(..., description="Product UUID to favorite")
 
class FavoriteResponse(BaseModel):
    """Response model for favorite."""
    id: UUID
    user_id: UUID
    product_id: UUID
    created_at: str
    
    class Config:
        from_attributes = True  # Allows creation from ORM models
 
# FastAPI validates automatically
@router.post("/")
async def create(dto: AddFavoriteDto):  # Pydantic validates dto
    # dto.product_id is guaranteed to be a valid UUID
    pass
```

---

## Anti-Patterns to Avoid

**Hard-coding test data:**
```python
# Bad
if user_id == "test-123":
    return mock_data

# Good
return await self.repository.find_by_user(user_id)
```

**Over-engineering:**
```python
# Bad - Adding features not in spec
class FavoritesCache:
    # Complex caching logic not requested
    pass

# Good - Simple, direct implementation
async def add_favorite(self, user_id: UUID, product_id: UUID) -> Favorite:
    return await self.repository.add(user_id, product_id)
```

**Ignoring existing patterns:**
```python
# Bad - Using different error handling than rest of codebase
return {"error": "Not found"}

# Good - Using same error classes as rest of codebase
raise NotFoundError('Product', str(product_id))
```

**Missing error handling:**
```python
# Bad
async def add_favorite(self, user_id: UUID, product_id: UUID) -> Favorite:
    return await self.repository.add(user_id, product_id)
    # What if repository raises?

# Good
async def add_favorite(self, user_id: UUID, product_id: UUID) -> Favorite:
    try:
        return await self.repository.add(user_id, product_id)
    except ConflictError:
        # Handle duplicate
        raise
    except Exception as e:
        # Log and re-raise
        logger.error(f"Failed to add favorite: {e}")
        raise
```

---

## Troubleshooting

### Tests Failing Unexpectedly

**Issue:** Tests pass individually but fail when run together
**Solution:** Tests are not isolated - check for shared state
```python
# Bad: Shared state
test_user: User = None
 
@pytest.fixture(scope="module")
def user():
    return create_user()
 
# Good: Fresh state per test
@pytest.fixture
def user():
    return create_user()
```

**Issue:** Async tests timing out
**Solution:** Missing await
```python
# Bad
async def test_save_user():
    service.save(user)  # Missing await
 
# Good
async def test_save_user():
    await service.save(user)
```

### Type Errors

**Issue:** "Incompatible types" or "Missing type annotation"
**Solution:** Check imports and type definitions
```python
# Ensure correct import
from app.models import User
 
# Ensure type hint is correct
user: User = await repository.find_by_id(user_id)
 
# Use Optional for nullable values
from typing import Optional
user: Optional[User] = await repository.find_by_id(user_id)
```

### Integration Issues

**Issue:** Dependency injection not working
**Solution:** Verify dependencies are properly configured
```python
# FastAPI example - use Depends()
from fastapi import Depends
 
def get_repository(session: AsyncSession = Depends(get_session)):
    return FavoritesRepository(session)
 
@router.post("/")
async def create(
    repo: FavoritesRepository = Depends(get_repository)
):
    # repo is injected automatically
    pass
```
