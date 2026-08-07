import { defineStore } from 'pinia'
import { api } from '../api/client'
import type { ChatMessage, Decision, Session, ToolExecution } from '../types'

export const useChatStore = defineStore('chat', {
  state: () => ({
    sessions: [] as Session[],
    selectedId: '' as string,
    messages: [] as ChatMessage[],
    decisions: [] as Decision[],
    toolExecutions: [] as ToolExecution[],
    loading: false,
  }),
  getters: {
    selected: (state) => state.sessions.find((item) => item.id === state.selectedId),
  },
  actions: {
    async refreshSessions() {
      this.sessions = await api.sessions()
      if (!this.selectedId && this.sessions[0]) this.selectedId = this.sessions[0].id
    },
    async select(id: string) {
      this.selectedId = id
      await this.refreshCurrent()
    },
    async refreshCurrent() {
      if (!this.selectedId) return
      ;[this.messages, this.decisions, this.toolExecutions] = await Promise.all([
        api.messages(this.selectedId),
        api.decisions(this.selectedId),
        api.toolExecutions(this.selectedId),
      ])
    },
    async deleteSession(id: string) {
      await api.deleteSession(id)
      this.sessions = this.sessions.filter((item) => item.id !== id)
      if (this.selectedId === id) {
        this.selectedId = this.sessions[0]?.id ?? ''
        this.messages = []
        this.decisions = []
        this.toolExecutions = []
        if (this.selectedId) await this.refreshCurrent()
      }
    },
  },
})
