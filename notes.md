[Agent registration API (preview)](https://learn.microsoft.com/en-us/microsoft-365/copilot/extensibility/api/admin-settings/agent-registration/overview) 
best for registering and managing registrations of agent cards.

[Agent Registry API](https://learn.microsoft.com/en-us/microsoft-agent-365/admin/graph-api) seems best for retrieval of card for already registered agents. 

Not clear which API should be used here. Documentation does not clearly state that all details from agent card are returned by either API. *agentRegistrations API* docs do say it uses agent card but does not specify whether all data from the card is registered. But, the *packages API* may return more data about agent status (approval, risk, etc.)

| API | Conceptual layer | Purpose | Status |
|---|---|---|---
| /copilot/agentRegistrations | Registration object | Manage an individual agent's registration, metadata, and card. CRUD but **no** List! | Preview
| /copilot/admin/catalog/packages | Tenant inventory/catalog | Discover what agents are present and available in the tenant | Released


### Questions

1. Planning to use /atalog/packages. What may I be missing?
2. Which API to use to create Entra artifacts only (no setup, registry)? Entra PS module or Graph?


