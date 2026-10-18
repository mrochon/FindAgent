## Query and response examples

Following are sample example requests executed though an agent (Claude Client) using the FindAgent MCP server.

### Publishers
**Query**

```
Which publishers have agents? List the top 5 by agent count.
```

**Response**

```
Here are the top 5 publishers by agent count:

#	Publisher	Agents
1	Microsoft Corporation	30
2	Apps 365 & HR365	10
3	GlobalData Plc	5
4	Loopio	4
5	OpenText Americas	4
Loopio and OpenText are tied at 4 agents. Other publishers may also have 4 and simply fell outside the top 5.
```
### Employee onboarding
**Query**

```
Use agent-finder to find agents that help with new hires.
```

**Response**

Two agents in the catalog are built specifically for new-hire onboarding, and a few others help with it.

**Built for onboarding**

| Agent	| Publisher	| What it does | Hosts |
|---|---|---|---|
| Day One Employee Onboarding | Kainos Software | Answers new hires' questions about company policy and gives role-specific guidance, using your connected M365 content (SharePoint and similar). It links to approved resources and points people to the right colleagues.	|Copilot |
| Onboarding 365 AI Agent |	Apps 365 & HR365 |	Automates onboarding tasks, tracks each new hire's progress and completion status, and answers HR questions. It's aimed more at HR teams running onboarding.| Teams, Copilot |

Also useful for new hires:

- **HR AI Agent** (Apps 365 & HR365): a general HR assistant that covers onboarding, tasks and learning.
- **Directory 365 AI Agent** (Apps 365 & HR365): lets new hires find people, departments, reporting lines and locations.
- **NexAA** (TVS Next): learning and development courses, leave and timesheets.
- **Gloat Copilot Agent** (Gloat): mentor matching and learning recommendations.
- **Partner Onboarding** (Datamatics): mainly onboards vendors and partners, though it mentions employees. It handles document upload, classification and approvals.

**Notes**

- None of these agents has any active users in the last 30 days, so nobody in your org is using them yet.
- If your onboarding material already lives in M365, **Day One Employee Onboarding** is the simplest choice, because it works from that content and doesn't need a third-party HR system.
- If HR wants to track onboarding tasks and progress, look at **Onboarding 365 AI Agent**. It likely needs the HR365 platform, so check before choosing it.

