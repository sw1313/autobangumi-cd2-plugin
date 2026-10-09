import { ref } from 'vue';

export const repairOwnerId = ref(0);

const repairCooldownMs = 5000;
const repairSentAt = new Map<string, number>();
const repairInflight = new Set<string>();

let nextRepairId = 1;

export function createRepairSeat() {
  return nextRepairId++;
}

export function claimRepairHashes(hashes: string[]) {
  const now = Date.now();
  const accepted: string[] = [];
  const cooled: string[] = [];
  for (const hash of hashes) {
    const sentAt = repairSentAt.get(hash);
    if (
      repairInflight.has(hash) ||
      (sentAt !== undefined && now - sentAt < repairCooldownMs)
    ) {
      cooled.push(hash);
      continue;
    }
    repairSentAt.set(hash, now);
    repairInflight.add(hash);
    accepted.push(hash);
  }
  return { accepted, cooled };
}

export function finishRepairHashes(hashes: string[]) {
  for (const hash of hashes) repairInflight.delete(hash);
}

export function releaseRepairHashes(hashes: string[]) {
  for (const hash of hashes) {
    repairInflight.delete(hash);
    repairSentAt.delete(hash);
  }
}
