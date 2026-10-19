# Compliance Notes

_Last updated: 2026-10-18_

---

## 1. PCI-DSS Scope

### Scope reduction
Booking Agent **never** touches raw payment card data. All card data is entered
directly in the payment gateway's hosted fields (Razorpay/Stripe). Our
integration receives only an opaque `payment_intent_id` returned after the
customer completes the payment widget on the gateway's origin.

| What we store              | PCI scope? |
|---------------------------|-----------|
| `payment_intent_id`       | No — opaque reference, not cardholder data |
| PAN / card number         | Never stored, processed, or transmitted |
| CVV / security code       | Never stored, processed, or transmitted |
| Cardholder name           | Not stored (passenger name stored separately, encrypted) |

**Our SAQ type:** SAQ-A (card data never touches our servers).

### Controls already in place
- `DuffelBalanceGateway` uses Duffel Balance for test orders; no card data involved.
- `FakePaymentGateway` used in development/CI; produces no real transactions.
- `BookingService.confirm()` calls the gateway before calling the supplier, so
  a failed payment never creates a PNR.
- All logs are scrubbed by `observability/log_scrubber.py` before reaching any sink.
- `BookingRecord.passengers_encrypted` stores passenger PII with Fernet
  symmetric encryption. The key is read from `BOOKING_ENCRYPTION_KEY` env var
  and must be rotated via secrets manager.

---

## 2. India Digital Personal Data Protection (DPDP) Act 2023

### Data collected
| Category            | Fields                                 | Retention |
|--------------------|----------------------------------------|-----------|
| Booking passengers | given_name, family_name, DOB, passport | 7 years (tax/audit) |
| Search queries     | Origin, destination, dates, pax counts | 90 days |
| Auth tokens (hash) | SHA-256 of API key                     | Never persisted beyond session |

### Consent
- Passengers provide data via a travel agent (the tenant), who is the Data
  Fiduciary under DPDP.
- The tenant's terms of service must include a DPDP-compliant consent notice
  covering: purpose (flight booking), categories of data, third-party sharing
  (GDS, airlines), retention period, and deletion rights.
- We expose a deletion API (not yet implemented; tracked as technical debt) so
  tenants can delete a passenger's data on request.

### Security safeguards
- Passenger PII encrypted at rest with Fernet (AES-128-CBC + HMAC-SHA256).
- Encryption key in secrets manager, rotatable without downtime.
- PII never logged (log scrubber patterns cover passport, PAN, email, phone).
- Database column `passengers_encrypted` stores only ciphertext.
- Access control: only `BookingService` holds the decryption key; HTTP routes
  never return raw passenger records, only `passenger_count`.

### Cross-border transfers
- GDS and airline APIs may process data outside India. Contractual safeguards
  (DPA / SCCs) must be in place before connecting to each supplier.
- Duffel (UK/EU): covered by GDPR adequacy / SCCs.
- Amadeus: DPA signed at enterprise contract stage.

### Data breach notification
- DPDP requires notification to the Data Protection Board within **72 hours**
  of becoming aware of a personal data breach.
- Incident response playbook: TBD (tracked in security backlog).

---

## 3. GDPR (for EU passengers)

### Lawful basis
- Contractual necessity: processing passenger data is necessary to fulfil the
  flight booking contract.

### Rights
| Right                  | Implementation |
|-----------------------|----------------|
| Right of access        | Tenant must expose this via their UI; we expose raw data via admin API to tenant |
| Right to erasure       | Delete endpoint on `BookingRecord` (not yet implemented; 90-day retention on SearchLog auto-purges) |
| Data portability       | Exportable via booking API JSON |
| Right to rectification | New booking required (cannot amend after PNR creation) |

### Data retention (GDPR Article 5(1)(e))
- `SearchLog`: 90 days, then auto-deleted (batch job TBD).
- `BookingRecord` + `passengers_encrypted`: 7 years (tax/audit obligation), then
  overwritten with zeroed ciphertext.
- `OfferSnapshot`: 24 hours max (configurable via `OFFER_SNAPSHOT_TTL`).

### Processor vs Controller
- Booking Agent is a Data Processor acting on behalf of the travel agent (Data
  Controller).
- A Data Processing Agreement (DPA) must be in place with every tenant.

---

## 4. Passenger Data Encryption

### Algorithm
- Library: `cryptography` (PyPI) — Fernet (AES-128-CBC + HMAC-SHA256).
- Key: 32-byte URL-safe base64 string stored in `BOOKING_ENCRYPTION_KEY` env var
  (or AWS Secrets Manager in production).
- Key rotation: generate new key, re-encrypt all records in a background job,
  update secret. Fernet supports multi-key decryption during transition.
- Ephemeral key for CI: `encryption.py` generates an in-process key when env
  var is absent — records encrypted this way are not durable across restarts.

### Access control
- `decrypt_passengers()` is only called inside `BookingService`.
- HTTP routes (`/v1/bookings/*`) never call `decrypt_passengers()`; they return
  only `passenger_count`.
- Future: add field-level access audit log (who decrypted, when).

---

## 5. GDS Credentials and API Key Security

### Production
- All supplier API keys stored in AWS Secrets Manager.
- Path convention: `booking-agent/{env}/{tenant_id}/{key}`.
- Each tenant has isolated credentials; no shared GDS access tokens.
- `AwsSecretsManagerProvider` caches secrets with a 5-minute TTL to avoid
  rate-limiting the Secrets Manager API.
- Key rotation: supported via `AwsSecretsManagerProvider.invalidate(key)`.

### CI/CD
- `EnvSecretsProvider` reads from env vars injected by CI secrets store.
- No real credentials committed to the repository.
- `.env.example` contains placeholder values only.

---

## 6. Authentication and Authorization

### API keys
- SHA-256 hashed before storage; plain keys never persisted.
- Per-tenant API keys; one compromised key cannot access other tenants' data.
- Rate limiting: 60 requests / minute per tenant (configurable via env vars).

### Tenant isolation
- Every repository query (`list_by_tenant`, `get`) filters by `tenant_id`.
- `AuthMiddleware` validates the key and sets `request.state.tenant_id` before
  any route handler executes.
- `_require_tenant()` on booking routes double-checks state is set.

---

## 7. Open Issues / Technical Debt

| Issue                                          | Priority |
|-----------------------------------------------|---------|
| DPDP/GDPR passenger deletion endpoint         | High    |
| Search/offer data auto-purge batch job         | High    |
| Security incident response playbook           | High    |
| Full pen-test before production launch        | High    |
| PNR time-limit auto-cancel batch job          | Medium  |
| Paid-but-not-ticketed reconciliation job      | Medium  |
| Dependency SBOM (pip-audit in CI)             | Medium  |
| Redis-backed rate limiter (multi-instance)    | Low     |
| Field-level decryption audit log              | Low     |
