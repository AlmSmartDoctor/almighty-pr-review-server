import { render, screen } from "@testing-library/react";
import { expect, test } from "vitest";
import { EnvStatus, type DeepHealth } from "./env-status";

const healthy: DeepHealth = {
  ok: true,
  gh: { installed: true, authenticated: true, login: "tester", error: null },
  claude: { installed: true },
  codex: { installed: true },
  db: { ok: true },
};

test("shows login when environment is healthy", async () => {
  render(<EnvStatus load={async () => healthy} />);
  expect(await screen.findByText("환경 정상 · tester")).toBeInTheDocument();
});

test("shows gh problem when unauthenticated", async () => {
  render(
    <EnvStatus
      load={async () => ({
        ...healthy,
        ok: false,
        gh: {
          installed: true,
          authenticated: false,
          login: null,
          error: "HTTP 401",
        },
      })}
    />,
  );
  expect(await screen.findByText("gh 미인증")).toBeInTheDocument();
});

test("shows vendor problem when both CLIs missing", async () => {
  render(
    <EnvStatus
      load={async () => ({
        ...healthy,
        ok: false,
        claude: { installed: false },
        codex: { installed: false },
      })}
    />,
  );
  expect(await screen.findByText("벤더 CLI 없음")).toBeInTheDocument();
});

test("shows unreachable when the request fails", async () => {
  render(
    <EnvStatus
      load={async () => {
        throw new Error("down");
      }}
    />,
  );
  expect(await screen.findByText("서버 연결 안 됨")).toBeInTheDocument();
});
