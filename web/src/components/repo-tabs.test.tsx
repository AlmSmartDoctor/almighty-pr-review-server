import { fireEvent, render, screen } from "@testing-library/react";
import { expect, test, vi } from "vitest";
import { RepoTabs } from "./repo-tabs";

const items = [
  { key: "전체", count: 3 },
  { key: "acme/api", count: 2 },
  { key: "acme/web", count: 1 },
];

test("connects tabs to their panel and keeps one tab in the tab order", () => {
  render(<RepoTabs items={items} activeKey={null} onSelect={() => undefined} panelId="repo-panel" />);

  const tabs = screen.getAllByRole("tab");
  expect(tabs[0]).toHaveAttribute("tabindex", "0");
  expect(tabs[0]).toHaveAttribute("aria-controls", "repo-panel");
  expect(tabs[1]).toHaveAttribute("tabindex", "-1");
});

test("supports arrow, home, and end keyboard navigation", () => {
  const onSelect = vi.fn();
  render(<RepoTabs items={items} activeKey="전체" onSelect={onSelect} panelId="repo-panel" />);

  const tabs = screen.getAllByRole("tab");
  tabs[0].focus();
  fireEvent.keyDown(tabs[0], { key: "ArrowRight" });
  expect(onSelect).toHaveBeenLastCalledWith("acme/api");
  expect(tabs[1]).toHaveFocus();

  fireEvent.keyDown(tabs[1], { key: "End" });
  expect(onSelect).toHaveBeenLastCalledWith("acme/web");
  expect(tabs[2]).toHaveFocus();

  fireEvent.keyDown(tabs[2], { key: "Home" });
  expect(onSelect).toHaveBeenLastCalledWith("전체");
  expect(tabs[0]).toHaveFocus();
});
