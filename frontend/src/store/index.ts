import { configureStore } from '@reduxjs/toolkit'
import createSagaMiddleware from 'redux-saga'
import jobsReducer from './slices/jobsSlice'
import uiReducer from './slices/uiSlice'
import { rootSaga } from './rootSaga'

const sagaMiddleware = createSagaMiddleware()

export const store = configureStore({
  reducer: {
    jobs: jobsReducer,
    ui: uiReducer,
  },
  middleware: (getDefaultMiddleware) => getDefaultMiddleware().concat(sagaMiddleware),
})

sagaMiddleware.run(rootSaga)

export type RootState = ReturnType<typeof store.getState>
export type AppDispatch = typeof store.dispatch
