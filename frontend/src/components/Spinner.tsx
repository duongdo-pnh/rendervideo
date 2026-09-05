import { Loader2 } from 'lucide-react'

export function Spinner({ className }: { className?: string }) {
  return <Loader2 className={`animate-spin ${className ?? 'h-4 w-4'}`} />
}
