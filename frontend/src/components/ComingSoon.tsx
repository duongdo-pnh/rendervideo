import { Card } from './Card'
import { PageHeader } from './PageHeader'

export function ComingSoon({ title }: { title: string }) {
  return (
    <>
      <PageHeader title={title} />
      <Card>
        <p className="text-sm text-slate-500">Trang này đang được xây dựng lại.</p>
      </Card>
    </>
  )
}
