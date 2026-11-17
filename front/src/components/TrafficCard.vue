<script setup>
/**
 * 交通参考卡片：把后端 TrafficOut 渲染成结构化交通信息。
 * 严格遵循 docs/02 输出粒度：公交(transit)带换乘 legs，其余三档只有耗时；
 * 数据来源（真实 API / 模拟）必须显式标注，绝不伪装。
 */
import { computed } from 'vue'

const props = defineProps({
  traffic: { type: Object, required: true },
  shops: { type: Array, default: () => [] },   // 用于把 shop_id 映射回店名/编号
})

const MODE_META = {
  driving: { icon: '🚗', label: '驾车' },
  cycling: { icon: '🚲', label: '骑行' },
  walking: { icon: '🚶', label: '步行' },
  transit: { icon: '🚌', label: '公交' },
}

const shopName = (sid) => {
  const s = props.shops.find(x => x.shop_id === sid)
  return s ? `${s.rank}. ${s.shop_name}` : sid
}

const fmtMin = (sec) => (sec == null ? '—' : `${Math.round(sec / 60)}分钟`)

// 固定顺序展示四档
const orderedModes = (modes) => ['driving', 'cycling', 'walking', 'transit']
  .filter(m => modes && modes[m] && modes[m].available !== false)

// 公交 legs：只展示 transit 段的线路名，步行段用小字
const transitLegs = (p) => (p?.legs || []).filter(l => l.kind === 'transit')

const statusText = computed(() => {
  const map = {
    ok: '正常', partial: '部分可用', unavailable: '暂不可用',
    need_location: '需要起点', invalid_coord: '坐标无效', fallback_static: '静态兜底',
  }
  return map[props.traffic.status] || props.traffic.status
})
</script>

<template>
  <div class="traffic-card">
    <div class="traffic-head">
      <span class="traffic-title">🚌 实时交通参考</span>
      <span class="traffic-status" :class="traffic.real_data ? 'traffic-real' : 'traffic-mock'">
        {{ traffic.real_data ? '真实路况' : '模拟数据' }}
      </span>
      <span class="traffic-state">状态：{{ statusText }}</span>
    </div>

    <div class="traffic-warn" v-if="!traffic.real_data">
      当前未配置真实地图 key，以下交通为演示用模拟数据，非真实路况。
    </div>
    <div class="traffic-warn" v-else-if="traffic.message">
      {{ traffic.message }}
    </div>

    <div class="traffic-origin" v-if="traffic.origin">
      出发地：{{ traffic.origin.display_name || `${traffic.origin.lat}, ${traffic.origin.lon}` }}
      <span v-if="traffic.origin.source" class="traffic-origin-src">（{{ traffic.origin.source }}）</span>
    </div>

    <div class="traffic-shop" v-for="s in traffic.shops" :key="s.shop_id">
      <div class="traffic-shop-name">{{ shopName(s.shop_id) }}</div>
      <div class="traffic-modes">
        <div class="traffic-mode" v-for="m in orderedModes(s.modes)" :key="m">
          <span class="traffic-mode-label">{{ MODE_META[m].icon }} {{ MODE_META[m].label }}</span>
          <span class="traffic-mode-dur">{{ fmtMin(s.modes[m].duration_sec) }}</span>
          <template v-if="m === 'transit'">
            <span class="traffic-transit-extra" v-if="s.modes[m].transfer_count != null">
              {{ s.modes[m].transfer_count }} 次换乘
            </span>
            <span class="traffic-transit-extra" v-if="s.modes[m].fare_yuan != null">
              ¥{{ s.modes[m].fare_yuan }}
            </span>
          </template>
        </div>
      </div>
      <!-- 公交换乘明细：只在 transit 档渲染 -->
      <div class="traffic-legs" v-if="s.modes.transit && transitLegs(s.modes.transit).length">
        <span class="traffic-legs-label">线路</span>
        <span class="traffic-leg" v-for="(leg, i) in transitLegs(s.modes.transit)" :key="i">
          {{ leg.line_name }}<small v-if="leg.station_count"> · {{ leg.station_count }}站</small>
        </span>
      </div>
    </div>

    <div class="traffic-foot" v-if="traffic.latency_ms">
      富化耗时 {{ Math.round(traffic.latency_ms) }}ms · 由地图服务在检索后单独计算，不属于餐厅资料
    </div>
  </div>
</template>
