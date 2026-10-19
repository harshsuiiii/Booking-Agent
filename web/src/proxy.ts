import { NextResponse } from "next/server";
import type { NextRequest } from "next/server";

/**
 * Optimistic route protection: signed-out visitors go to /login before any app page
 * renders. The API still checks every request (expired or forged cookies get a 401,
 * which the app turns into a "please sign in again" redirect).
 *
 * The cookie is set by the API on localhost:8000; cookies ignore ports, so this app on
 * localhost:3000 sees it too. In production, serve the API on the same site.
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
  matcher: ["/((?!_next/static|_next/image|favicon.ico|.*\\.(?:png|svg|ico|jpg|webp)$).*)"],
};
