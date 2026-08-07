import { createApp } from 'vue'
import { createPinia } from 'pinia'
import { createRouter, createWebHistory } from 'vue-router'
import App from './App.vue'
import ChatView from './views/ChatView.vue'
import ProfilesView from './views/ProfilesView.vue'
import PersonaView from './views/PersonaView.vue'
import SettingsView from './views/SettingsView.vue'
import SchedulesView from './views/SchedulesView.vue'
import LogsView from './views/LogsView.vue'
import ChainsView from './views/ChainsView.vue'
import BlacklistView from './views/BlacklistView.vue'
import LearningResourcesView from './views/LearningResourcesView.vue'
import PluginStatusView from './views/PluginStatusView.vue'
import { bootstrapControlSession } from './api/client'
import './styles.css'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', component: ChatView },
    { path: '/profiles', component: ProfilesView },
    { path: '/persona', component: PersonaView },
    { path: '/schedules', component: SchedulesView },
    { path: '/chains', component: ChainsView },
    { path: '/learning', component: LearningResourcesView },
    { path: '/plugins', component: PluginStatusView },
    { path: '/blacklist', component: BlacklistView },
    { path: '/settings', component: SettingsView },
    { path: '/logs', component: LogsView },
  ],
})

bootstrapControlSession()
  .catch(reason => console.error('控制面会话初始化失败', reason))
  .finally(() => createApp(App).use(createPinia()).use(router).mount('#app'))
