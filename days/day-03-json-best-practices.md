---
date: 2026-07-22
day: 03
title: "JSON Best Practices: Structure, Versioning, Error Responses"
tags: [json, api-design, rest, data-structure, versioning]
---

TL;DR
- **Structure**: Use consistent naming (camelCase/snake_case), nest objects logically, avoid empty arrays/nulls
- **Versioning**: Version your API via URL (`/v1/`, `/v2/`) or headers (`Accept: application/vnd.api+json;version=2`)
- **Error Responses**: Follow standard patterns (HTTP status + error object with code, message, details)

---

## 1. JSON Structure Best Practices

### Naming Conventions
Pick **one** convention and stick with it:

```json
{
  "userName": "alice",
  "createdAt": "2026-07-22T10:30:00Z",
  "isActive": true
}
```

OR

```json
{
  "user_name": "alice",
  "created_at": "2026-07-22T10:30:00Z",
  "is_active": true
}
```

**Recommendation**: Use `camelCase` for JSON (JavaScript/web standard), but be consistent across your entire API.

### Logical Nesting
Group related data; avoid flat structures:

❌ **Bad:**
```json
{
  "userId": 123,
  "userName": "alice",
  "userEmail": "alice@example.com",
  "userRole": "admin",
  "addressStreet": "123 Main St",
  "addressCity": "Boston",
  "addressZip": "02101"
}
```

✅ **Good:**
```json
{
  "id": 123,
  "name": "alice",
  "email": "alice@example.com",
  "role": "admin",
  "address": {
    "street": "123 Main St",
    "city": "Boston",
    "zip": "02101"
  }
}
```

### Handling Empty/Null Values

❌ **Bad:**
```json
{
  "tags": [],
  "bio": null,
  "avatar": null
}
```

✅ **Good:**
```json
{
  "tags": ["python", "api", "json"],
  "bio": "Software engineer",
  "avatar": "https://..."
}
```

**Rules**:
- Omit optional fields if not applicable (preferred) OR include with `null`
- Never include empty arrays; either omit or populate
- Use ISO 8601 for dates: `"2026-07-22T10:30:00Z"`

### Pagination
Always provide metadata for paginated responses:

```json
{
  "data": [
    { "id": 1, "name": "Item 1" },
    { "id": 2, "name": "Item 2" }
  ],
  "pagination": {
    "page": 1,
    "pageSize": 10,
    "total": 42,
    "totalPages": 5
  },
  "links": {
    "self": "/api/v1/items?page=1",
    "next": "/api/v1/items?page=2",
    "last": "/api/v1/items?page=5"
  }
}
```

---

## 2. API Versioning

### Strategy 1: URL Path Versioning (Most Common)

```
GET /api/v1/users
GET /api/v2/users
```

**Pros**: Clear, easy to test, human-readable
**Cons**: Multiple versions to maintain

```python
# Flask example
@app.route('/api/v1/users', methods=['GET'])
def get_users_v1():
    return jsonify({
        "users": [...],
        "format": "v1"  # Different schema
    })

@app.route('/api/v2/users', methods=['GET'])
def get_users_v2():
    return jsonify({
        "data": [...],  # Different response structure
        "meta": {...}
    })
```

### Strategy 2: Header Versioning

```
GET /api/users
Accept: application/vnd.myapi+json;version=1
```

**Pros**: Clean URLs, backwards compatible
**Cons**: Less obvious, harder to test in browser

```python
# Flask example
@app.route('/api/users', methods=['GET'])
def get_users():
    version = request.headers.get('Accept-Version', '1')
    
    if version == '2':
        return jsonify({
            "data": [...],
            "meta": {...}
        })
    else:
        return jsonify({
            "users": [...]
        })
```

### Strategy 3: Parameter Versioning

```
GET /api/users?apiVersion=2
```

**Pros**: Optional, backward compatible
**Cons**: Easy to forget, clutters query strings

### Best Practice: Deprecation Timeline

```json
{
  "data": {...},
  "deprecation": {
    "sunset": "2027-01-01",
    "alternative": "/api/v2/users",
    "message": "v1 will be removed on 2027-01-01. Please migrate to v2."
  }
}
```

Or use HTTP header:
```
Deprecation: true
Sunset: Wed, 21 Dec 2027 23:59:59 GMT
Link: </api/v2/users>; rel="successor-version"
```

---

## 3. Error Response Patterns

### Standard Error Response Format

```json
{
  "error": {
    "code": "VALIDATION_ERROR",
    "message": "Request validation failed",
    "statusCode": 400,
    "details": [
      {
        "field": "email",
        "issue": "Invalid email format",
        "value": "not-an-email"
      },
      {
        "field": "age",
        "issue": "Must be >= 18",
        "value": 15
      }
    ],
    "requestId": "req_abc123def456",
    "timestamp": "2026-07-22T10:30:00Z"
  }
}
```

### HTTP Status Codes

| Code | Use Case | Example |
|------|----------|---------|
| 400 | Bad Request | Invalid JSON, validation failed |
| 401 | Unauthorized | Missing/invalid auth token |
| 403 | Forbidden | Authenticated but no permission |
| 404 | Not Found | Resource doesn't exist |
| 409 | Conflict | Duplicate record, state conflict |
| 422 | Unprocessable Entity | Semantically invalid (detailed validation) |
| 429 | Too Many Requests | Rate limit exceeded |
| 500 | Internal Server Error | Unexpected server error |

### Error Response Examples

**400 Bad Request:**
```json
{
  "error": {
    "code": "INVALID_REQUEST",
    "message": "Request body is not valid JSON",
    "statusCode": 400
  }
}
```

**401 Unauthorized:**
```json
{
  "error": {
    "code": "UNAUTHORIZED",
    "message": "Missing or invalid authentication token",
    "statusCode": 401,
    "hint": "Include 'Authorization: Bearer YOUR_TOKEN' header"
  }
}
```

**422 Unprocessable Entity:**
```json
{
  "error": {
    "code": "VALIDATION_FAILED",
    "message": "One or more fields failed validation",
    "statusCode": 422,
    "details": [
      {
        "field": "password",
        "code": "TOO_SHORT",
        "message": "Password must be at least 8 characters",
        "constraints": { "minLength": 8 }
      },
      {
        "field": "username",
        "code": "ALREADY_EXISTS",
        "message": "Username is already taken"
      }
    ]
  }
}
```

**429 Rate Limited:**
```json
{
  "error": {
    "code": "RATE_LIMIT_EXCEEDED",
    "message": "Too many requests",
    "statusCode": 429,
    "retryAfter": 60
  }
}
```

### Python Implementation (Flask)

```python
from flask import Flask, jsonify, request
from datetime import datetime
import uuid

app = Flask(__name__)

def error_response(code, message, status_code, details=None, **kwargs):
    """Generate a standardized error response."""
    return jsonify({
        "error": {
            "code": code,
            "message": message,
            "statusCode": status_code,
            "details": details,
            "requestId": str(uuid.uuid4()),
            "timestamp": datetime.utcnow().isoformat() + "Z",
            **kwargs
        }
    }), status_code

@app.route('/api/v1/users', methods=['POST'])
def create_user():
    try:
        data = request.get_json()
        
        # Validation
        errors = []
        if not data or 'email' not in data:
            errors.append({
                "field": "email",
                "issue": "Required field missing"
            })
        elif '@' not in data.get('email', ''):
            errors.append({
                "field": "email",
                "issue": "Invalid email format"
            })
        
        if errors:
            return error_response(
                "VALIDATION_ERROR",
                "Request validation failed",
                422,
                details=errors
            )
        
        # Process request
        return jsonify({
            "data": {"id": 123, "email": data['email']},
            "message": "User created successfully"
        }), 201
    
    except Exception as e:
        return error_response(
            "INTERNAL_ERROR",
            "An unexpected error occurred",
            500
        )

@app.errorhandler(404)
def not_found(e):
    return error_response(
        "NOT_FOUND",
        "The requested resource was not found",
        404
    )

@app.errorhandler(401)
def unauthorized(e):
    return error_response(
        "UNAUTHORIZED",
        "Authentication required",
        401
    )
```

---

## 4. Comprehensive Example: User API

### Success Response (200 OK)
```json
{
  "data": {
    "id": 123,
    "name": "Alice",
    "email": "alice@example.com",
    "role": "admin",
    "createdAt": "2026-01-15T08:30:00Z",
    "updatedAt": "2026-07-22T10:30:00Z"
  },
  "meta": {
    "version": "1.0",
    "requestId": "req_xyz789"
  }
}
```

### List Response with Pagination (200 OK)
```json
{
  "data": [
    { "id": 1, "name": "Alice", "role": "admin" },
    { "id": 2, "name": "Bob", "role": "user" }
  ],
  "pagination": {
    "page": 1,
    "pageSize": 10,
    "total": 42,
    "hasMore": true
  },
  "links": {
    "self": "/api/v1/users?page=1",
    "next": "/api/v1/users?page=2"
  }
}
```

### Error Response (422 Unprocessable Entity)
```json
{
  "error": {
    "code": "VALIDATION_ERROR",
    "message": "Validation failed",
    "statusCode": 422,
    "details": [
      {
        "field": "email",
        "code": "INVALID_FORMAT",
        "message": "Invalid email format"
      },
      {
        "field": "age",
        "code": "OUT_OF_RANGE",
        "message": "Age must be between 18 and 120"
      }
    ],
    "requestId": "req_abc123",
    "timestamp": "2026-07-22T10:30:00Z"
  }
}
```

---

## Links & Resources

- [JSON:API Specification](https://jsonapi.org/) — standardized JSON API format
- [RFC 7231 HTTP Status Codes](https://tools.ietf.org/html/rfc7231#section-6) — official HTTP status definitions
- [ISO 8601 Date Format](https://en.wikipedia.org/wiki/ISO_8601) — standard for date/time
- [RESTful API Design Best Practices](https://restfulapi.net/) — comprehensive guide
- [Problem Details RFC 7807](https://tools.ietf.org/html/rfc7807) — standardized error responses
- [Google API Design Guide](https://cloud.google.com/apis/design) — industry best practices

---

## Next Steps / Reflections

- Implement a small Flask/FastAPI app with proper error handling
- Build a versioning strategy and deprecation timeline
- Test error responses with different status codes
- Consider using JSON Schema for validation
- Explore OpenAPI/Swagger for API documentation
