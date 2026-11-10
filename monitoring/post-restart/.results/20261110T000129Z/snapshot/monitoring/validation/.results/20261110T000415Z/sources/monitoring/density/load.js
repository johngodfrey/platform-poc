import { Gauge } from 'k6/metrics';
import { options as sharedOptions, order, timeline as sharedTimeline } from '../footprint/footprint.js';
export { order };
export const options = sharedOptions;
const cap = new Gauge('density_memory_limit_bytes');
const slots = new Gauge('density_projected_slots');
const cpu = new Gauge('density_max_node_cpu_fraction');
export function timeline() {
  sharedTimeline();
  cap.add(Number(__ENV.MEMORY_MIB) * 1048576);
  slots.add(Math.floor(Number(__ENV.POOL_MIB) / Number(__ENV.MEMORY_MIB)));
  cpu.add(Number(__ENV.MAX_NODE_CPU));
}
