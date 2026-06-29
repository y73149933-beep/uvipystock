import { Input } from "@/components/common/Input";

interface QuantityInputProps {
  value: string;
  onChange: (value: string) => void;
  baseAsset?: string;
  available?: string;
  onUseMax?: () => void;
}

export function QuantityInput({
  value,
  onChange,
  baseAsset = "BTC",
  available,
  onUseMax,
}: QuantityInputProps) {
  return (
    <div>
      <div className="mb-1 flex items-center justify-between">
        <label className="text-xs font-medium text-gray-400">Quantity</label>
        {available && (
          <button
            onClick={onUseMax}
            className="text-xs text-accent hover:underline"
          >
            Max: {parseFloat(available).toFixed(6)} {baseAsset}
          </button>
        )}
      </div>
      <Input
        type="number"
        step="0.0001"
        min="0"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder="0.0000"
        suffix={baseAsset}
      />
    </div>
  );
}
