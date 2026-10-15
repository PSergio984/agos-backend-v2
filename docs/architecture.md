# Backend Architecture

## Overview

The AGOS backend is a FastAPI application serving as the central API for both the admin dashboard and the responder PWA. It ingests data from IoT sensors, cameras, and weather APIs, processes it through ML and fusion analysis, and broadcasts real-time updates via WebSocket.

## Three-Layer Architecture

```
Endpoints (app/api/v1/endpoints/)
    │
    ▼
Services (app/services/)
    │
    ▼
CRUD (app/crud/)
    │
    ▼
Database (PostgreSQL via async SQLAlchemy)
```

**Endpoints** — Thin route handlers. Declare auth dependencies, validate input via Pydantic schemas, delegate to services, return response models.

**Services** — Business logic. Instantiated as module-level singletons (e.g., `responder_app_service = ResponderAppService()`). Handle orchestration, external API calls, data transformation.

**CRUD** — Data access. Extend `CRUDBase` generic class. Each CRUD class is a singleton exported from `app/crud/__init__.py`. Use async SQLAlchemy with `select()`, `joinedload()`, and window functions.

## Registration Pattern

New features must be registered in multiple `__init__.py` files:

| What | Register in |
|------|------------|
| CRUD class | `app/crud/__init__.py` |
| Service class | `app/services/__init__.py` |
| Pydantic schema | `app/schemas/__init__.py` |
| Router | `app/api/v1/router.py` (include_router) |

## Authentication

Four auth levels:

| Level | Mechanism | Dependency | Used by |
|-------|-----------|------------|---------|
| Admin (read) | JWT Bearer | `Depends(require_auth)` | Most admin endpoints |
| Admin (superuser write) | JWT Bearer + superuser | `Depends(require_superuser)` | Admin user creation, deactivation, reactivation |
| IoT device | API key header | `Depends(require_iot_api_key)` | Sensor readings, frame upload |
| Responder | JWT Bearer with `type=responder` claim | `Depends(require_responder_auth)` | Responder app endpoints, push subscription |

Admin JWT tokens use 15-minute access tokens plus 7-day refresh tokens. Responder JWT tokens are issued after OTP verification and expire after 90 days. Passwords and OTPs are hashed with Argon2.

## Real-Time Data Flow

### WebSocket Architecture

Two WebSocket endpoints:

1. **`/ws?location_id={id}&token={jwt}`** — Frontend/responder clients. `token` is optional: a valid responder token pins the channel to that responder's effective site (active site, else home) and `location_id` is ignored; an invalid token closes 1008. Unauthenticated clients choose the channel via `location_id`.
2. **`/ws/rpi?location_id={id}&camera_device_id={id}`** — Raspberry Pi camera. Sends binary frames for ML inference. `camera_device_id` is optional — legacy firmware sends `location_id` only and the site's default active camera is resolved.

Both ingest paths verify the camera↔site binding (registered `location_id` must match) and reject devices on inactive sites.

### Message Types

| Type | Source | Description |
|------|--------|-------------|
| `sensor_update` | `POST /sensor-readings/record` | Water level reading + calculated summary |
| `blockage_detection_update` | ML inference on camera frame | Blockage status + percentage |
| `weather_update` | Scheduled weather fetch (APScheduler) | Weather conditions from OpenMeteo |
| `fusion_analysis_update` | Any of the above triggers recalculation | Combined risk score |
| `camera_update` | `POST /stream/upload-image` or `WS /ws/rpi` binary frame | Base64 JPEG frame for the admin live camera panel |
| `alert_claimed` | `POST /responder/claim` | A responder took a blockage alert — sent to the dispatch's site channel (every channel for a NULL-site announcement) |

### Data Ingestion → Broadcast Flow

```
IoT Sensor ──POST /sensor-readings/record──► SensorReadingService
                                                │
                                                ├──► Store in DB
                                                ├──► Calculate summary (water level, trend, alert)
                                                ├──► Update fusion state
                                                └──► WebSocket broadcast (sensor_update + fusion_analysis_update)

Camera ──WS /ws/rpi or POST /stream/upload-image──► MLService
                                        │
                                        ├──► Throttle (2-min interval per camera)
                                        ├──► Broadcast raw frame as camera_update
                                        ├──► Run inference (blockage detection)
                                        ├──► Upload image to Cloudinary
                                        ├──► Store ModelReading in DB
                                        ├──► Update fusion state
                                        └──► WebSocket broadcast (blockage_detection_update + fusion_analysis_update)

APScheduler ──every N minutes──► WeatherService
                                    │
                                    ├──► Fetch from OpenMeteo API
                                    ├──► Store in DB
                                    ├──► Update fusion state
                                    └──► WebSocket broadcast (weather_update + fusion_analysis_update)
```

## Multi-Site Model

A **location** is a monitored site. Everything operational is scoped to one:

- **Responders** have a home site (`location_id`) and an optional active site (`active_location_id`, presence override — home or an open deployment site only).
- **Deployments** (`responder_deployments`) are admin-managed temporary postings: `starts_at`/`planned_ends_at`/`ends_at`. A partial unique index allows at most one open deployment per responder; opening a new one supersedes the current one.
- **Dispatches** carry `location_id` — detection-created alerts go only to responders whose effective site matches; NULL-site dispatches are announcements fanned out to every active site. Alert feeds, unread counts, and claim authorization are all scoped to the responder's effective site.
- **Groups** are per-site (`groups.location_id`); `(name, location_id)` is unique with NULLS NOT DISTINCT, so each site gets its own `All Active Responders` group on responder activation.
- **Devices** (`sensor_devices`, `camera_devices`) bind to a site with `is_active` flags; ingest is rejected for inactive devices and inactive sites. Legacy camera firmware without `camera_device_id` resolves the site's default active camera.
- **Caches** key on device/site and are invalidated when the registry or site record changes.

## Notification System

### Push Notification Flow

1. Admin triggers notification → `POST /push/send-notification`
2. `NotificationService` creates `NotificationDispatch` record (site-scoped when triggered from detection; admin-sent pushes are NULL-site announcements)
3. Looks up active responders in scope (effective site match, or everyone for NULL-site) with push subscriptions
4. Sends Web Push to each subscription via `pywebpush`
5. Creates `NotificationDelivery` records (PENDING → SENT/FAILED)
6. Responder's service worker receives push → native notification
7. Responder acknowledges → `POST /responder/acknowledge-alert`, or claims the alert → `POST /responder/claim`

### SMS OTP Flow

1. Responder enters phone → `POST /responder/for-approval`
2. Service generates 6-digit OTP, hashes with Argon2, stores with 10-min expiry
3. Sends plaintext OTP via `SMSService` → Android SMS Gateway (SMSGate app)
4. Responder enters OTP → `POST /responder/verify-otp`
5. Service verifies hash, checks expiry and attempt count (max 5)
6. On success: activate responder, add to default group, delete OTP record, return a 90-day responder JWT

## Scheduled Jobs

| Job | Schedule | Description |
|-----|----------|-------------|
| Weather fetch | Configurable interval | Fetches from OpenMeteo, stores, broadcasts |
| Daily summary | Midnight (UTC+8) | Aggregates sensor, model, weather data per location |
| Data cleanup | 1:00 AM (UTC+8) | Purges old data based on retention settings |
| Alert escalation | Every 5 minutes | Re-notifies unacknowledged critical alert deliveries |

## Caching

`CacheService` provides in-memory caching for frequently accessed, rarely changing data:

- Device IDs (sensor, camera)
- Location coordinates
- Sensor configuration (thresholds)
- Alert thresholds

Cache is populated on first access and invalidated on config updates, device
registry writes (create/update/rebind/deactivate), and site create/update.

## External Integrations

| Service | Purpose | Module |
|---------|---------|--------|
| OpenMeteo | Weather data | `app/services/weather/api_client.py` |
| Cloudinary | Image storage (blockage frames) | `app/services/upload_service.py`, `app/services/ml_service.py` |
| Groq | LLM analysis of daily summaries (SSE) | `app/services/analysis_service.py` |
| SMSGate | SMS OTP delivery via Android phone | `app/services/sms_service.py` |
| Web Push | VAPID-based push notifications | `app/services/notification_service.py` |

## Rate Limiting

| Endpoint | Limit | Reason |
|----------|-------|--------|
| `POST /auth/login` | 5/minute | Brute force prevention |
| `POST /auth/refresh` | 10/minute | Token churn throttle |
| `POST /auth/change-password` | 3/minute | Brute force prevention |
| `POST /sensor-readings/record` | 60/minute | IoT device throttle |
| `POST /stream/upload-image` | 35/minute | Camera frame throttle |
| `POST /responder/for-approval` | 5/minute | OTP abuse prevention |
| `POST /responder/resend-otp/{id}` | 3/minute | OTP resend abuse prevention |
| `POST /responder/verify-otp` | 5/minute | OTP brute force prevention |
| `POST /responder/send-sms` | 10/minute | SMS cost throttle |
| `POST /responder/claim` | 20/minute | Claim spam prevention |
| `POST /public/evacuation-route` | 20/minute | External API cost |
| `POST,DELETE /push/subscribe-citizen` | 20/minute | Subscription churn |
| `POST /analysis/reports`, `POST .../stream` | 6/minute | LLM cost throttle |
| `GET /analysis/reports/{id}/pdf` | 10/minute | Report generation |

## File Structure

```
app/
├── api/v1/
│   ├── endpoints/          # 26 route handler files
│   └── router.py           # Central route registration
├── core/
│   ├── config.py           # Pydantic settings (env vars)
│   ├── database.py         # Async engine + session factory
│   ├── security.py         # JWT + Argon2
│   └── exceptions.py       # Custom SMS exceptions
├── crud/                   # 24 CRUD classes + CRUDBase
├── models/
│   ├── data_sources/       # Location, SensorDevice, SensorReading, Weather, etc.
│   └── responder_related/  # Responder, Group, NotificationDelivery, etc.
├── schemas/
│   └── reading_summary_response/  # WebSocket response types
└── services/
    ├── daily_summary/      # Daily aggregation + risk scoring
    ├── responder/          # Admin management + responder self-service
    ├── responder_group/    # Group CRUD + validation
    ├── sensor_reading/     # Recording, trends, export
    ├── stream/             # Camera frame ingestion helpers
    └── weather/            # OpenMeteo fetch, persistence, scheduling
```
