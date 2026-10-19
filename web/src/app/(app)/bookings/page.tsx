import BookingsTable from "@/components/BookingsTable";
import { PageHeader } from "@/components/ui";

export default function BookingsPage() {
  return (
    <div className="flex flex-col gap-6">
      <PageHeader
        title="Bookings"
        subtitle="Every booking for your agency, including ones made from Search and the Assistant."
      />
      <BookingsTable pageSize={20} />
    </div>
  );
}
