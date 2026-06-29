import { Button } from "@/components/common/Button";
import { useOrdersStore } from "@/store/ordersStore";

interface BulkActionsBarProps {
  onEditSelected?: () => void;
}

export function BulkActionsBar({ onEditSelected }: BulkActionsBarProps) {
  const selectedIds = useOrdersStore((s) => s.selectedIds);
  const cancelSelected = useOrdersStore((s) => s.cancelSelected);
  const cancelAll = useOrdersStore((s) => s.cancelAll);

  const selectedCount = selectedIds.size;

  return (
    <div className="flex items-center gap-2">
      {selectedCount > 0 && (
        <>
          <span className="text-xs text-gray-400">{selectedCount} selected</span>
          {onEditSelected && (
            <Button variant="outline" size="sm" onClick={onEditSelected}>
              Edit
            </Button>
          )}
          <Button variant="danger" size="sm" onClick={cancelSelected}>
            Cancel Selected
          </Button>
        </>
      )}
      <Button
        variant="ghost"
        size="sm"
        onClick={() => cancelAll()}
        className="ml-auto"
      >
        Cancel All
      </Button>
    </div>
  );
}
