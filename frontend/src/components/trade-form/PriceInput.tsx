import { Input } from "@/components/common/Input";

interface PriceInputProps {
  value: string;
  onChange: (value: string) => void;
  disabled?: boolean;
  quoteAsset?: string;
}

export function PriceInput({ value, onChange, disabled, quoteAsset = "USDT" }: PriceInputProps) {
  return (
    <Input
      label="Price"
      type="number"
      step="0.01"
      min="0"
      value={value}
      onChange={(e) => onChange(e.target.value)}
      disabled={disabled}
      placeholder="0.00"
      suffix={quoteAsset}
    />
  );
}
