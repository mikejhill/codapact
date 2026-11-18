# Examples

| Folder | API | Contract | Credentials | Use |
| --- | --- | --- | --- | --- |
| `petstore` | Toy pet store | Local file | Optional bearer | Offline tests and docs |
| `library` | Toy library | Local GraphQL schema | None | Offline tests and docs |
| `github` | GitHub REST | Fetched from github/rest-api-description | Optional token | Live, read-heavy |
| `jira` | Jira Cloud REST v3 | Fetched from developer.atlassian.com | Email + API token | Live, your own site |
| `confluence` | Confluence Cloud REST v2 | Fetched from developer.atlassian.com | Email + API token | Live, your own site |
| `swagger-petstore` | Swagger Petstore v3 | Fetched from swagger-api/swagger-petstore | None needed | Live, public demo that accepts writes |

Each live example generates a coda the way a user would, from its folder:

```sh
uvx codapact generate                                   # writes ./generated
uvx --from ./generated codapact-github doctor           # program name = distribution name
uvx --from ./generated codapact-github describe --brief
```

Jira and Confluence contracts carry a placeholder host. Point them at your site first:
`JIRA_BASE_URL=https://SITE.atlassian.net` or
`CONFLUENCE_BASE_URL=https://SITE.atlassian.net/wiki/api/v2`.

`generated/` folders are not committed.
