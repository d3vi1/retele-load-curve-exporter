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

Resolved for the direct HTTP client: after Visualforce login and Salesforce
frontdoor session establishment, the client can load the Lightning shell and parse
the `/s/sfsites/l/{urlencoded JSON context}/bootstrap.js?...` URL from
`/s/new-load-curves-client`.

The successful direct bootstrap sequence is:

1. Keep the shell/bootstrap-URL context as the outgoing `aura.context`.
2. POST `aura://ComponentController/ACTION$getApplication` to
   `/s/sfsites/aura?r=0&aura.Component.getApplication=1` with:
   - `aura.context` set to the shell context;
   - `aura.pageURI` set to `/s/new-load-curves-client`;
   - `aura.token` set to `undefined`.
3. Store the top-level `token` returned by getApplication.
4. Use that token with the original shell context for POD discovery.

Important live-probe finding: getApplication also returns a top-level `context`,
but using that returned/extended context for POD discovery caused HTTP 400. Using
the returned token with the original shell/bootstrap-URL context succeeded: POD
discovery returned HTTP 200 with Aura actions `SUCCESS` / `SUCCESS` and sanitized
POD-shaped records. The implementation therefore keeps response contexts separate
from the outgoing request context until a future response context is proven
compatible.
