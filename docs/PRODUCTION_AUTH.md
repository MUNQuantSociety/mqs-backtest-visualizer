# Production sign-in configuration

The web application uses Amazon Cognito authorization code with PKCE. Cognito
handles passwords, email verification and password recovery. The API verifies
access tokens and maps the issuer and subject to an internal `app.users` UUID.
Existing `public.user_creds` passwords and temporary-user reports are not migrated
or linked by email.

## Deployment configuration

The production resources are in AWS account `855603407903`, region `us-east-2`.
The identifiers below are public configuration, not credentials.

| Setting | Value |
| --- | --- |
| User pool | `us-east-2_3ZrRAFaoC` |
| Issuer | `https://cognito-idp.us-east-2.amazonaws.com/us-east-2_3ZrRAFaoC` |
| Public app client | `62ncmo6n5ksdqs7se3njih4m7v` |
| Sign-in domain | `https://mqs-backtest-855603407903.auth.us-east-2.amazoncognito.com` |
| Frontend | `https://backtest.munquantsociety.com` |
| API base | `https://api.munquantsociety.com/api` |
| Amplify app / branch | `d25khc1s0qwjp8` / `main` |
| ECS cluster / service | `mqs-backtest-visualizer` / `mqs-backtest-visualizer-api` |

The ECS `api` container needs:

```dotenv
AUTH_COGNITO_ISSUER=https://cognito-idp.us-east-2.amazonaws.com/us-east-2_3ZrRAFaoC
AUTH_COGNITO_CLIENT_ID=62ncmo6n5ksdqs7se3njih4m7v
AUTH_ALLOW_DEV_IDENTITY=false
APP_ENV=production
CORS_ORIGINS=http://localhost:3000,http://localhost:5173,https://backtest.munquantsociety.com
```

Amplify build environment:

```dotenv
VITE_API_BASE_URL=https://api.munquantsociety.com/api
VITE_API_TIMEOUT=30000
VITE_USE_FIXTURES=false
VITE_AUTH_AUTHORITY=https://cognito-idp.us-east-2.amazonaws.com/us-east-2_3ZrRAFaoC
VITE_AUTH_CLIENT_ID=62ncmo6n5ksdqs7se3njih4m7v
VITE_AUTH_DOMAIN=https://mqs-backtest-855603407903.auth.us-east-2.amazoncognito.com
```

Vite embeds these values when building. Changes to Amplify variables require a
new build. Keep the SPA rewrite to `/index.html` so direct requests to
`/auth/callback` and application routes reach the frontend router.

## Cognito settings

- The client has no secret and allows only the OAuth authorization-code grant.
  The browser generates the PKCE verifier and S256 challenge.
- Allowed scopes are `openid email profile`.
- Callback URLs are exactly
  `https://backtest.munquantsociety.com/auth/callback` and
  `http://localhost:5173/auth/callback`.
- Logout URLs are the same two origins with `/auth/login`.
- Access and ID tokens last 15 minutes; refresh tokens last one day. Token
  revocation is enabled. Logout clears the application session and cached data.
  An already issued JWT can remain valid at the API until expiry; the API checks
  its signature and claims rather than calling Cognito on every request.
- Email sign-in is case insensitive. Self-registration requires verification of
  the email address, and recovery uses verified email. Password policy requires
  at least 12 characters with uppercase, lowercase, number and symbol.
- The pool uses Cognito's Lite plan and classic hosted sign-in pages, with
  Cognito-managed email delivery. MFA is not configured.

Do not put AWS credentials, client secrets or bearer tokens into `VITE_*`
variables. A production `X-User-Id` header cannot authenticate a user.

## Release and verification

Backend merges to `main` run CI and deploy to the existing ECS service. The
deployment clones the current task definition, preserving these runtime settings.
Frontend `main` builds automatically in Amplify. Deploy the verified backend
before releasing the frontend login integration.

Verify the exact hosted-origin CORS response, login and callback, authenticated
`GET /api/auth/me`, empty history for a new account, owner isolation, session
restoration and logout. Health and strategy catalogue metadata remain public;
private report statistics must not be returned by public catalogue endpoints.

The ALB still has the existing source-IP restriction for the preview. Real
sign-in does not itself remove that restriction or sandbox uploaded Python
strategies. Preserve the restriction until those separate access and execution
controls are reviewed. The task's port 8000 accepts traffic only from the ALB.
