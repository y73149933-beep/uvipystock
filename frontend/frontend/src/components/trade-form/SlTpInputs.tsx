import { Input } from "@/components/common/Input";

interface SLTPInputsProps {
  slEnabled: boolean;
  tpEnabled: boolean;
  slStopPrice: string;
  slLimitPrice: string;
  tpLimitPrice: string;
  onToggleSL: (enabled: boolean) => void;
  onToggleTP: (enabled: boolean) => void;
  onSLStopPriceChange: (v: string) => void;
  onSLLimitPriceChange: (v: string) => void;
  onTPLimitPriceChange: (v: string) => void;
}

export function SLTPInputs({
  slEnabled,
  tpEnabled,
  slStopPrice,
  slLimitPrice,
  tpLimitPrice,
  onToggleSL,
  onToggleTP,
  onSLStopPriceChange,
  onSLLimitPriceChange,
  onTPLimitPriceChange,
}: SLTPInputsProps) {
  return (
    <div className="space-y-2 border-t border-border pt-2">
      <div className="flex items-center gap-2">
        <input
          type="checkbox"
          checked={slEnabled}
          onChange={(e) => onToggleSL(e.target.checked)}
          className="h-3 w-3 accent-ask"
        />
        <span className="text-xs font-medium text-ask">Stop-Loss</span>
      </div>
      {slEnabled && (
        <div className="grid grid-cols-2 gap-2 pl-5">
          <Input
            type="number"
            step="0.01"
            value={slStopPrice}
            onChange={(e) => onSLStopPriceChange(e.target.value)}
            placeholder="Stop price"
            suffix="USDT"
          />
          <Input
            type="number"
            step="0.01"
            value={slLimitPrice}
            onChange={(e) => onSLLimitPriceChange(e.target.value)}
            placeholder="Limit (opt)"
            suffix="USDT"
          />
        </div>
      )}

      <div className="flex items-center gap-2">
        <input
          type="checkbox"
          checked={tpEnabled}
          onChange={(e) => onToggleTP(e.target.checked)}
          className="h-3 w-3 accent-bid"
        />
        <span className="text-xs font-medium text-bid">Take-Profit</span>
      </div>
      {tpEnabled && (
        <div className="pl-5">
          <Input
            type="number"
            step="0.01"
            value={tpLimitPrice}
            onChange={(e) => onTPLimitPriceChange(e.target.value)}
            placeholder="TP limit price"
            suffix="USDT"
          />
        </div>
      )}
    </div>
  );
}
