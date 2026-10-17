import { screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { BuntzenWatch } from "@/mt/watch-schema";
import { BUNTZEN_INFO, FIXTURE_NOW, makeBuntzenWatch, stateWith, fail } from "@/test/fixtures";
import { renderWithProviders } from "@/test/render";
import { BuntzenDetail } from "./BuntzenDetail";

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(FIXTURE_NOW); // Wed Jun 30 2027, 7:31 am
});

afterEach(() => {
  vi.useRealTimers();
});

const show = (watch: BuntzenWatch) =>
  renderWithProviders(<BuntzenDetail watch={watch} state={stateWith([watch])} info={BUNTZEN_INFO} />);

describe("a Buntzen Lake watch's page", () => {
  it("puts Book on Yodel first, full width, then the passes, the timeline and how booking works", () => {
    show(makeBuntzenWatch());
    const book = screen.getByRole("link", { name: "Book on Yodel" });
    const passes = screen.getByRole("list", { name: "Passes" });
    expect(book.compareDocumentPosition(passes) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(book).toHaveClass("w-full");
    expect(screen.getByText(/^Checked 7:31 am\. On sale Wed Jun 30 at 7:00 am;/)).toBeInTheDocument();
    expect(screen.getAllByText(/Checked 7:31 am/)).toHaveLength(1); // said once
  });

  it("says an uncertain pass may be bookable", () => {
    const base = makeBuntzenWatch();
    show(makeBuntzenWatch({ options: [{ ...(base.options[0] ?? fail()), status: "maybe", detail: "Waitlist only" }] }));
    expect(
      screen.getByText("Yodel shows a status we don't recognise; it may be bookable. Check on Yodel now."),
    ).toBeInTheDocument();
  });

  it("says when passes go on sale, before they do, with nothing to book", () => {
    show(
      makeBuntzenWatch({
        spec: { date: "2027-07-03", passes: ["full_day"] },
        check: {
          state: "waiting_release",
          opens_at: 1_814_536_800,
          outcome: "pending",
          checked_at: null,
          last_check_at: null,
        },
        options: [],
      }),
    );
    expect(screen.getByText("On sale Fri 7:00 am")).toBeInTheDocument();
    expect(
      screen.getByText(
        "Passes for Sat Jul 3 go on sale Fri Jul 2 at 7:00 am. We'll start checking then, and remind you at 6:55.",
      ),
    ).toBeInTheDocument();
    expect(screen.getByText(/^Not checked yet\./)).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /Book/ })).toBeNull();
  });
});
