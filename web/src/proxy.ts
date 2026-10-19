import { NextResponse } from "next/server";
import type { NextRequest } from "next/server";

/**
 * Optimistic route protection: signed-out visitors go to /login before any app page
 * renders. The API still checks every request (expired or forged cookies get a 401,
 * which the app turns into a "please sign in again" redirect).
 *
 * The cookie is set by the API. Locally that is localhost:8000 (cookies ignore ports, so
 * localhost:3000 sees it); deployed, /v1 is forwarded to the API, so it is this site's.
 * /v1 itself is skipped: API calls (login included) must reach the API untouched.
 */
const SESSION_COOKIE = "ba_session";

export function proxy(request: NextRequest) {
  const { pathname, search } = request.nextUrl;
  if (pathname === "/login") return NextResponse.next();
  if (!request.cookies.has(SESSION_COOKIE)) {
    const url = new URL("/login", request.url);
    url.searchParams.set("next", pathname + search);
    return NextResponse.redirect(url);
  }
  return NextResponse.next();
}

export const config = {
  matcher: ["/((?!v1/|_next/static|_next/image|favicon.ico|.*\\.(?:png|svg|ico|jpg|webp)$).*)"],
};
