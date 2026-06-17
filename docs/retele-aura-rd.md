# Rețele Electrice Aura R&D

This file records sanitized findings for the direct HTTP client. It must not contain
credentials, session IDs, real Aura tokens, raw POD lists, raw responses, or personal data.

## Confirmed POD Discovery Request

Captured from an authenticated Chrome DevTools session while loading:

`/s/new-load-curves-client`

Endpoint:

`POST /s/sfsites/aura?r=4&other.PED_Search_My_POD_.getNumPOD=1&other.PED_Search_My_POD_.searchDBVisualizzaFornitura=1`

Content type:

`application/x-www-form-urlencoded; charset=UTF-8`

Required form fields:

- `message`
- `aura.context`
- `aura.pageURI`
- `aura.token`

`aura.pageURI`:

`/s/new-load-curves-client`

`aura.context` keys observed:

- `app`
- `dn`
- `fwuid`
- `globals`
- `loaded`
- `mode`
- `uad`

`aura.token`:

- length observed: 334
- stored by Lightning in browser local storage under:
  `$AuraClientService.token$siteforce:communityApp`
- base64-url decodes to a JSON prefix plus a 32-byte signature
- JSON prefix fields observed: `nonce`, `typ`, `alg`, `kid`, `crit`, `iat`, `exp`
- token is not portable across a separate HTTP login session

Message actions:

```json
{
  "actions": [
    {
      "descriptor": "apex://PED_Search_My_POD_Controller/ACTION$searchDBVisualizzaFornitura",
      "callingDescriptor": "markup://c:PED_SearchPOD_Functionality",
      "params": {
        "county": "",
        "city": "",
        "POD": "",
        "power": "",
        "distributioncompany": ""
      }
    },
    {
      "descriptor": "apex://PED_Search_My_POD_Controller/ACTION$getNumPOD",
      "callingDescriptor": "markup://c:PED_SearchPOD_Functionality",
      "params": {}
    }
  ]
}
```

## Current Blocker

The direct HTTP client can complete Visualforce login and establish the Salesforce
frontdoor session. It can also load the Lightning shell. However, direct Aura calls
need a session-specific server-signed Aura token. Copying a token captured from a
browser session into a separate HTTP login session returns an Aura error rather than
POD records.

Remaining R&D:

1. Identify the exact server response or Aura bootstrap operation that creates
   `$AuraClientService.token$siteforce:communityApp`.
2. Reproduce that bootstrap in the Python HTTP client for the same cookie/session jar.
3. Use the confirmed POD discovery message above.

