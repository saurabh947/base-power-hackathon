import { describe, expect, it } from 'vitest';
import { applyAdoption, applyRecovery, blockOf, cohortOf, defaultOwners } from './owners.ts';

describe("controllers' batteries (a replica of the backend's ownership)", () => {
  it('start as contiguous blocks: workers 0-6 Houston, 7-14 Dallas–Fort Worth, 17 spans San Antonio and Austin', () => {
    const owners = defaultOwners(20_000, 20);
    expect(owners[0]).toBe(0);
    expect(owners[6_999]).toBe(6);
    expect(owners[7_000]).toBe(7);
    expect(owners[14_999]).toBe(14);
    expect(owners[17_000]).toBe(17);
    expect(owners[17_999]).toBe(17);
    expect(owners[19_999]).toBe(19);
    expect(cohortOf(owners, 17)).toHaveLength(1000);
  });

  it('split like numpy.array_split when the fleet does not divide evenly', () => {
    const owners = defaultOwners(10, 4); // chunks of 3, 3, 2, 2
    expect(Array.from(owners)).toEqual([0, 0, 0, 1, 1, 1, 2, 2, 3, 3]);
  });

  it('deal a dead controller out round-robin in id order, as coordinator._reassign does', () => {
    const owners = defaultOwners(12, 3); // 0: 0-3, 1: 4-7, 2: 8-11
    const cohort = applyAdoption(owners, 1, [0, 2]);
    expect(Array.from(cohort)).toEqual([4, 5, 6, 7]);
    expect(Array.from(owners)).toEqual([0, 0, 0, 0, 0, 2, 0, 2, 2, 2, 2, 2]);
    // A second crash hands over the adopted batteries too.
    expect(Array.from(applyAdoption(owners, 2, [0]))).toEqual([5, 7, 8, 9, 10, 11]);
    expect(Array.from(owners).every((w) => w === 0)).toBe(true);
  });

  it('leaves batteries orphaned when nobody can adopt them', () => {
    const owners = defaultOwners(4, 2);
    applyAdoption(owners, 0, []);
    expect(Array.from(owners)).toEqual([-1, -1, 1, 1]);
  });

  it('give a restarted controller its own block back (worker_recovered)', () => {
    expect(blockOf(20_000, 20, 17)).toEqual([17_000, 18_000]);
    expect(blockOf(10, 4, 1)).toEqual([3, 6]);
    const owners = defaultOwners(12, 3); // 0: 0-3, 1: 4-7, 2: 8-11
    applyAdoption(owners, 1, [0, 2]);
    applyRecovery(owners, 1, 3);
    expect(Array.from(owners)).toEqual([0, 0, 0, 0, 1, 1, 1, 1, 2, 2, 2, 2]);
    applyRecovery(owners, 7, 3); // not a controller: nothing changes
    expect(Array.from(owners)).toEqual([0, 0, 0, 0, 1, 1, 1, 1, 2, 2, 2, 2]);
  });
});
