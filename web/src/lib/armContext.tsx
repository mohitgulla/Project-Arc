/**
 * E15.7 (D69): which account the Tower is viewing, shared by every page and API hook.
 *
 * The URL is the source of truth (`?arm=XP-12:t1`; shareable). In-app links don't carry the
 * parameter, so the provider remembers the arm chosen in this page load and puts it back on
 * the URL after an in-app navigation drops it. Nothing is persisted: a fresh load without
 * `?arm` is always Arc Investor (owner). `?arm=control` is an explicit Arc Investor.
 */
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";
import type { ReactNode } from "react";
import { NavigationType, useLocation, useNavigate, useNavigationType } from "react-router-dom";

import { ARM_PARAM, parseArm, searchWithArm } from "./arm";

interface ArmState {
  /** `?arm=` value under view; `null` = Arc Investor. */
  arm: string | null;
  /** Switch to *arm* (`null` = back to Arc Investor), keeping the page and its other params. */
  setArm: (arm: string | null) => void;
}

const ArmContext = createContext<ArmState>({ arm: null, setArm: () => undefined });

function explicitParam(search: string): boolean {
  return new URLSearchParams(search).has(ARM_PARAM);
}

export function ArmProvider({ children }: { children: ReactNode }) {
  const location = useLocation();
  const navigate = useNavigate();
  const urlArm = parseArm(location.search);
  const explicit = explicitParam(location.search);
  const navType = useNavigationType();
  const [kept, setKept] = useState<string | null>(urlArm);
  // URL wins whenever it names the account (or on load / back-forward); an in-app link
  // without the param keeps the arm.
  const arm = explicit || navType === NavigationType.Pop ? urlArm : kept;

  const keptRef = useRef(kept);

  // Runs on URL changes only (not on `kept` changes, so a switch can't race its navigation).
  // Back/forward (POP) takes the URL as it is, so Back can return to Arc Investor.
  useEffect(() => {
    if (explicitParam(location.search) || navType === NavigationType.Pop) {
      keptRef.current = parseArm(location.search);
      setKept(keptRef.current);
      return;
    }
    const k = keptRef.current;
    if (k)
      navigate({ pathname: location.pathname, search: searchWithArm(location.search, k), hash: location.hash }, { replace: true });
  }, [location.pathname, location.search, location.hash, navType, navigate]);

  const setArm = useCallback(
    (next: string | null) => {
      keptRef.current = next;
      setKept(next);
      navigate({ pathname: location.pathname, search: searchWithArm(location.search, next), hash: location.hash });
    },
    [navigate, location.pathname, location.search, location.hash],
  );

  const value = useMemo(() => ({ arm, setArm }), [arm, setArm]);
  return <ArmContext.Provider value={value}>{children}</ArmContext.Provider>;
}

/** The account under view and the switch action. */
export function useArmState(): ArmState {
  return useContext(ArmContext);
}

/** The `?arm=` value every API call carries (`null` = Arc Investor, no parameter). */
export function useArm(): string | null {
  return useContext(ArmContext).arm;
}
