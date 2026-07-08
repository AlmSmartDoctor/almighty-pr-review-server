import { render, screen } from "@testing-library/react";
import { SettingsSection } from "./SettingsSection";

test("renders global defaults from settings", async () => {
  render(<SettingsSection load={async () => ({
    default_effort: "medium", concurrency_limit: 2,
    default_poll_interval: 60, approval_gate_on: 1,
    prescreen_model: "claude-haiku", prescreen_gate_threshold: "moderate",
  })} />);
  expect(await screen.findByDisplayValue("medium")).toBeInTheDocument();
  expect(screen.getByText(/동시성/)).toBeInTheDocument();
});
