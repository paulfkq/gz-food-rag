<script setup>
/** 单家店的展示卡片：把后端 ShopOut 的字段渲染成结构化信息。 */
defineProps({
  shop: { type: Object, required: true },
})
</script>

<template>
  <article class="shop-card">
    <div class="shop-head">
      <span class="rank-badge">{{ shop.rank }}</span>
      <div class="shop-title">
        <h3>{{ shop.shop_name }}</h3>
        <div class="shop-meta">
          <span v-if="shop.district || shop.business_area" class="meta-item">
            📍 {{ [shop.district, shop.business_area].filter(Boolean).join(' · ') }}
          </span>
          <span v-if="shop.cuisines && shop.cuisines.length" class="meta-item">
            🍽 {{ shop.cuisines.join(' / ') }}
          </span>
        </div>
      </div>
      <div class="shop-score">
        <strong>{{ shop.rating ?? '—' }}</strong>
        <small>评分</small>
      </div>
    </div>

    <div class="shop-stats">
      <div class="stat" v-if="shop.price_per_person != null">
        <span class="stat-label">人均</span>
        <span class="stat-value">¥{{ shop.price_per_person }}</span>
      </div>
      <div class="stat" v-if="shop.noise_level">
        <span class="stat-label">环境</span>
        <span class="stat-value">{{ shop.noise_level }}</span>
      </div>
      <div class="stat" v-if="shop.metro_walk_min != null">
        <span class="stat-label">地铁步行</span>
        <span class="stat-value">{{ shop.metro_walk_min }} 分钟</span>
      </div>
      <div class="stat" v-if="shop.nearest_metro && shop.nearest_metro.length">
        <span class="stat-label">最近站</span>
        <span class="stat-value">{{ shop.nearest_metro[0].station }} · {{ shop.nearest_metro[0].line }}</span>
      </div>
    </div>

    <div class="shop-tags">
      <span v-for="t in shop.scene_tags" :key="'s'+t" class="tag tag-scene">{{ t }}</span>
      <span v-for="t in shop.taste_tags" :key="'t'+t" class="tag tag-taste">{{ t }}</span>
      <span v-for="t in shop.matched_soft" :key="'m'+t" class="tag tag-matched">✓ {{ t }}</span>
    </div>

    <div class="shop-allergen" v-if="shop.allergens && shop.allergens.length">
      <span class="allergen-label">⚠ 过敏原</span>
      <span class="allergen-list">{{ shop.allergens.join('、') }}</span>
    </div>
  </article>
</template>
