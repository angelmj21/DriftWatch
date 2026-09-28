import styles from './SignaturePanel.module.css';

function SignatureList({ title, items, emptyLabel }) {
  return (
    <div className={styles.group}>
      <div className={styles.groupTitle}>{title}</div>
      {(!items || items.length === 0) && <div className={styles.empty}>{emptyLabel}</div>}
      {items?.map((s) => (
        <div key={s.signature} className={styles.row}>
          <span className={styles.sig}>{s.template || s.signature}</span>
          <span className={styles.count}>{s.count}</span>
        </div>
      ))}
    </div>
  );
}

// Props: { top: [{signature, template, count}], new: [...] }
export function SignaturePanel({ top = [], new: newSignatures = [] }) {
  return (
    <div className={styles.wrap}>
      <SignatureList title="Top signatures" items={top} emptyLabel="No signatures yet." />
      <SignatureList title="New signatures" items={newSignatures} emptyLabel="Nothing new." />
    </div>
  );
}
